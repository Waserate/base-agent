"""
aave_borrow.py — AAVE v3 borrow module for Base chain.

Flow:
  open_borrow  → acquire collateral → approve Pool → supply collateral →
                 setUserUseReserveAsCollateral(True) → compute borrow amount →
                 Pool.borrow() → swap borrow token -> ETH
  close_borrow → read actual vDebt balance → acquire repay×REPAY_BUFFER →
                 approve Pool → Pool.repay(MAX_UINT256) →
                 Pool.withdraw(MAX_UINT256) → swap collateral → ETH

State encoding (same as moonwell_borrow):
  "COLL_TOKEN:coll_wei||BORROW_TOKEN:borrow_wei"
  e.g. "WETH:2500000000000000||USDC:875000"

AAVE health factor: Pool.getUserAccountData(wallet).healthFactor / 1e18
  Aggregate for entire wallet (not per-position).

AAVE v3 Pool Base: 0xA238Dd80C259a72e81d7e4664a9801593F98d1c5
"""

import os, time, random, logging
from web3 import Web3
from dotenv import load_dotenv
import executor
import swap as _swap
from swap import PriceGuardError, ConfigError, SwapExecutionError

load_dotenv()
log = logging.getLogger(__name__)

DRY_RUN                = os.getenv('DRY_RUN', '').lower() in ('1', 'true', 'yes')
REPAY_BUFFER           = 1.005
HEALTH_CLOSE_THRESHOLD = 1.5
POOL_ADDR              = '0xA238Dd80C259a72e81d7e4664a9801593F98d1c5'
MAX_UINT256            = 2**256 - 1

# ── ABIs ───────────────────────────────────────────────────────────────────────

_POOL_ABI = [
    {'name': 'supply', 'type': 'function', 'stateMutability': 'nonpayable',
     'inputs': [
         {'name': 'asset',        'type': 'address'},
         {'name': 'amount',       'type': 'uint256'},
         {'name': 'onBehalfOf',   'type': 'address'},
         {'name': 'referralCode', 'type': 'uint16'},
     ], 'outputs': []},
    {'name': 'withdraw', 'type': 'function', 'stateMutability': 'nonpayable',
     'inputs': [
         {'name': 'asset',  'type': 'address'},
         {'name': 'amount', 'type': 'uint256'},
         {'name': 'to',     'type': 'address'},
     ], 'outputs': [{'name': '', 'type': 'uint256'}]},
    {'name': 'borrow', 'type': 'function', 'stateMutability': 'nonpayable',
     'inputs': [
         {'name': 'asset',            'type': 'address'},
         {'name': 'amount',           'type': 'uint256'},
         {'name': 'interestRateMode', 'type': 'uint256'},
         {'name': 'referralCode',     'type': 'uint16'},
         {'name': 'onBehalfOf',       'type': 'address'},
     ], 'outputs': []},
    {'name': 'repay', 'type': 'function', 'stateMutability': 'nonpayable',
     'inputs': [
         {'name': 'asset',            'type': 'address'},
         {'name': 'amount',           'type': 'uint256'},
         {'name': 'interestRateMode', 'type': 'uint256'},
         {'name': 'onBehalfOf',       'type': 'address'},
     ], 'outputs': [{'name': '', 'type': 'uint256'}]},
    {'name': 'setUserUseReserveAsCollateral', 'type': 'function', 'stateMutability': 'nonpayable',
     'inputs': [
         {'name': 'asset',           'type': 'address'},
         {'name': 'useAsCollateral', 'type': 'bool'},
     ], 'outputs': []},
    {'name': 'getUserAccountData', 'type': 'function', 'stateMutability': 'view',
     'inputs': [{'name': 'user', 'type': 'address'}],
     'outputs': [
         {'name': 'totalCollateralBase',         'type': 'uint256'},
         {'name': 'totalDebtBase',               'type': 'uint256'},
         {'name': 'availableBorrowsBase',        'type': 'uint256'},
         {'name': 'currentLiquidationThreshold', 'type': 'uint256'},
         {'name': 'ltv',                         'type': 'uint256'},
         {'name': 'healthFactor',                'type': 'uint256'},
     ]},
]

_VDEBT_ABI = [
    {'name': 'balanceOf', 'type': 'function', 'stateMutability': 'view',
     'inputs': [{'name': 'account', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'uint256'}]},
]


# ── Contract factory ───────────────────────────────────────────────────────────

def _pool():
    return executor.w3.eth.contract(
        address=Web3.to_checksum_address(POOL_ADDR), abi=_POOL_ABI
    )


# ── State encoding ─────────────────────────────────────────────────────────────

def encode_state(coll_token: str, coll_wei: int, borrow_token: str, borrow_wei: int) -> str:
    return f'{coll_token}:{coll_wei}||{borrow_token}:{borrow_wei}'


def decode_state(encoded: str) -> tuple:
    """Returns (coll_token: str, coll_wei: int, borrow_token: str, borrow_wei: int)."""
    coll_part, borrow_part = encoded.split('||')
    coll_token,  coll_wei_str   = coll_part.split(':')
    borrow_token, borrow_wei_str = borrow_part.split(':')
    return coll_token, int(coll_wei_str), borrow_token, int(borrow_wei_str)


# ── Health ─────────────────────────────────────────────────────────────────────

def check_health(encoded_state: str, p: dict) -> float:
    """
    Returns AAVE healthFactor (1e18-scaled on-chain → divided by 1e18).
    Returns 999.0 in DRY_RUN or when no debt (AAVE returns maxUint256).
    """
    if DRY_RUN:
        return 999.0
    try:
        data = _pool().functions.getUserAccountData(executor.WALLET).call()
        # data[4] = totalDebtBase (1e8-scaled USD); data[5] = healthFactor (1e18)
        if data[4] == 0:
            return 999.0
        raw = data[5] / 1e18
        return min(raw, 999.0)  # cap maxUint256/1e18 from zero-debt case
    except Exception as e:
        log.warning(f'check_health failed: {e}')
        return 999.0


# ── Borrow computation ─────────────────────────────────────────────────────────

def _compute_borrow_wei(p: dict, ltv: float, col_wei_override: int = 0) -> int:
    coll_price_usd   = executor.get_token_usd_price(p['collateral_token'])
    borrow_price_usd = executor.get_token_usd_price(p['borrow_token'])
    col_wei          = col_wei_override if col_wei_override > 0 else int(p['collateral_amount_wei'])
    coll_usd         = (col_wei / 10**p['collateral_decimals']) * coll_price_usd
    borrow_usd       = coll_usd * ltv
    borrow_amt       = borrow_usd / borrow_price_usd * 10**p['borrow_decimals']
    return int(borrow_amt)


# ── Open ───────────────────────────────────────────────────────────────────────

def open_borrow(p: dict, collateral_usd: float = 0.0) -> tuple:
    """
    Open AAVE v3 borrow position.
    Returns (encoded_state: str, borrow_tx_hash: str).

    Flow:
      1. Guard
      2. Extract config
      3. Pick random LTV
      4. Acquire collateral (wrap if WETH, else swap)
      5. Approve Pool for collateral
      6. supply() collateral to Pool
      7. setUserUseReserveAsCollateral(True)
      8. Compute borrow amount
      9. borrow() from Pool (variable rate, mode=2)
      10. Release borrow token → ETH

    collateral_usd: if >0, override config collateral_amount_wei from live price.
    """
    executor._guard()

    coll_token  = p['collateral_token']
    coll_addr   = Web3.to_checksum_address(p['collateral_address'])
    coll_wei    = int(p['collateral_amount_wei'])
    if collateral_usd > 0:
        _coll_price = executor.get_token_usd_price(coll_token)
        if _coll_price > 0:
            coll_wei = int(collateral_usd / _coll_price * 10**p['collateral_decimals'])
            log.info(f'  collateral_usd override ${collateral_usd:.2f} → {coll_wei/10**p["collateral_decimals"]:.6f} {coll_token}')

    borrow_token = p['borrow_token']
    borrow_addr  = Web3.to_checksum_address(p['borrow_address'])

    pool_addr = Web3.to_checksum_address(POOL_ADDR)
    pool      = _pool()

    ltv = random.uniform(float(p['ltv_min']), float(p['ltv_max']))
    log.info(f'aave open_borrow: coll={coll_token} {coll_wei}  LTV={ltv:.1%}  borrow={borrow_token}')

    # 4. Acquire collateral
    if DRY_RUN:
        log.info(f'[DRY RUN] acquire {coll_token} {coll_wei}')
    elif coll_token == 'WETH':
        _swap.wrap_eth(coll_wei)
    else:
        _swap.attempt_swap(_swap.swap_eth_to_token, coll_addr, coll_wei)

    time.sleep(2)

    # 5. Approve Pool for collateral
    executor._approve_if_needed(coll_addr, pool_addr, coll_wei)

    # 6. supply() collateral to Pool
    tx = pool.functions.supply(
        coll_addr, coll_wei, executor.WALLET, 0
    ).build_transaction(executor._tx_params())
    tx['gas'] = executor._gas_limit(tx)
    supply_txh = executor._send(tx)
    log.info(f'aave supply tx={supply_txh}')
    try:
        import step_logger as _sl
        _sl.slog('collateral', f'{coll_token}  TX {supply_txh[:10]}...', txhash=supply_txh)
    except Exception:
        pass

    time.sleep(3)

    # 7. setUserUseReserveAsCollateral(True)
    tx = pool.functions.setUserUseReserveAsCollateral(
        coll_addr, True
    ).build_transaction(executor._tx_params())
    tx['gas'] = executor._gas_limit(tx)
    col_txh = executor._send(tx)
    log.info(f'aave setCollateral tx={col_txh}')

    time.sleep(2)

    # 8. Compute borrow amount
    borrow_wei = _compute_borrow_wei(p, ltv, col_wei_override=coll_wei)
    log.info(f'aave borrow_wei={borrow_wei} ({borrow_token})')

    # 9. borrow() — variable rate mode=2
    tx = pool.functions.borrow(
        borrow_addr, borrow_wei, 2, 0, executor.WALLET
    ).build_transaction(executor._tx_params())
    tx['gas'] = executor._gas_limit(tx)
    borrow_txh = executor._send(tx)
    log.info(f'aave borrow tx={borrow_txh}')
    try:
        import step_logger as _sl
        bor_usd = (borrow_wei / 10**int(p.get('borrow_decimals', 6))) * executor.get_token_usd_price(borrow_token) if borrow_token not in ('USDC', 'USDS') else borrow_wei / 10**int(p.get('borrow_decimals', 6))
        _sl.slog('borrow', f'{borrow_token} ${bor_usd:.2f}  TX {borrow_txh[:10]}...', txhash=borrow_txh)
    except Exception:
        pass

    time.sleep(3)

    # 10. Release borrow token → ETH
    if DRY_RUN:
        log.info(f'[DRY RUN] release {borrow_token} → ETH')
    elif borrow_token == 'WETH':
        _swap.unwrap_all_weth()
    else:
        erc20 = executor.w3.eth.contract(
            address=borrow_addr, abi=executor.ERC20_ABI
        )
        bal = erc20.functions.balanceOf(executor.WALLET).call()
        if bal > 0:
            _swap.attempt_swap(_swap.swap_token_to_eth, borrow_addr, bal)

    return encode_state(coll_token, coll_wei, borrow_token, borrow_wei), borrow_txh


# ── Close ──────────────────────────────────────────────────────────────────────

def close_borrow(encoded_state: str, p: dict) -> str:
    """
    Close AAVE v3 borrow position.
    Returns final withdraw tx hash.

    Flow:
      1. Guard
      2. Decode state
      3. Read actual vDebt balance
      4. Compute repay_amount with buffer
      5. Acquire repay token
      6. Approve Pool for repay
      7. repay(MAX_UINT256) — repays full outstanding debt
      8. withdraw(MAX_UINT256) — withdraws all collateral
      9. Release collateral → ETH
      10. Sweep leftover borrow token → ETH (best-effort)
    """
    executor._guard()

    # 2. Decode state
    coll_token, coll_wei, borrow_token, borrow_wei = decode_state(encoded_state)

    coll_addr   = Web3.to_checksum_address(p['collateral_address'])
    borrow_addr = Web3.to_checksum_address(p['borrow_address'])
    vdebt_addr  = Web3.to_checksum_address(p['borrow_vdebt'])

    pool_addr = Web3.to_checksum_address(POOL_ADDR)
    pool      = _pool()

    # 3. Read actual debt from vDebt token
    if DRY_RUN:
        actual_debt = borrow_wei
        log.info(f'[DRY RUN] vDebt estimate={actual_debt}')
    else:
        vdebt_c     = executor.w3.eth.contract(address=vdebt_addr, abi=_VDEBT_ABI)
        actual_debt = vdebt_c.functions.balanceOf(executor.WALLET).call()
        if actual_debt == 0:
            log.warning(f'vDebt balanceOf=0 — position may already be closed; using encoded borrow_wei as fallback')
            actual_debt = borrow_wei

    # 4. Compute repay with buffer
    repay_amount = int(actual_debt * REPAY_BUFFER)
    log.info(f'aave close_borrow: repay {borrow_token} {repay_amount}  (debt={actual_debt})')

    # 5. Acquire repay token
    if DRY_RUN:
        log.info(f'[DRY RUN] acquire {borrow_token} {repay_amount}')
    elif borrow_token == 'WETH':
        _swap.wrap_eth(repay_amount)
    else:
        _swap.attempt_swap(_swap.swap_eth_to_token, borrow_addr, repay_amount)

    time.sleep(3)

    # 6. Approve Pool for repay
    executor._approve_if_needed(borrow_addr, pool_addr, repay_amount)

    # 7. repay(MAX_UINT256) — repays entire outstanding debt
    tx = pool.functions.repay(
        borrow_addr, MAX_UINT256, 2, executor.WALLET
    ).build_transaction(executor._tx_params())
    tx['gas'] = executor._gas_limit(tx)
    repay_txh = executor._send(tx)
    log.info(f'aave repay tx={repay_txh}')

    time.sleep(4)

    # 8. withdraw(MAX_UINT256) — withdraw all collateral
    tx = pool.functions.withdraw(
        coll_addr, MAX_UINT256, executor.WALLET
    ).build_transaction(executor._tx_params())
    tx['gas'] = executor._gas_limit(tx)
    withdraw_txh = executor._send(tx)
    log.info(f'aave withdraw tx={withdraw_txh}')

    time.sleep(4)

    # 9. Release collateral → ETH
    if DRY_RUN:
        log.info(f'[DRY RUN] release {coll_token} → ETH')
    elif coll_token == 'WETH':
        _swap.unwrap_all_weth()
    else:
        erc20 = executor.w3.eth.contract(address=coll_addr, abi=executor.ERC20_ABI)
        bal   = erc20.functions.balanceOf(executor.WALLET).call()
        if bal > 0:
            _swap.attempt_swap(_swap.swap_token_to_eth, coll_addr, bal)

    # 10. Sweep leftover borrow token → ETH (best-effort)
    if borrow_token != 'WETH' and not DRY_RUN:
        try:
            erc20    = executor.w3.eth.contract(address=borrow_addr, abi=executor.ERC20_ABI)
            leftover = erc20.functions.balanceOf(executor.WALLET).call()
            if leftover > 0:
                _swap.attempt_swap(_swap.swap_token_to_eth, borrow_addr, leftover)
        except Exception:
            pass

    return withdraw_txh
