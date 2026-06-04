"""
Compound v3 borrow module — Base chain.

Supports single-collateral and multi-collateral positions across 3 Comets:
  USDC Comet  0xb125... — borrow USDC  (util ~87%)
  WETH Comet  0x46e6... — borrow WETH  (util ~96%, max_util=0.98)
  AERO Comet  0x784e... — borrow AERO  (util ~21%)

State encoding in positions.amount_wei (stored as string):
  "{token}:{wei}|{token}:{wei}||{base}:{borrow_wei}"
  collateral pairs (|) before ||, borrow info after

  Single:  "WETH:2500000000000000||USDC:1200000"
  Multi:   "WETH:2500000000000000|wstETH:2000000000000000||USDC:2100000"

Platform config keys used (from contracts.json):
  comet_address   : Comet proxy address
  borrow_token    : symbol  (USDC / WETH / AERO)
  borrow_address  : ERC20 address of base token
  borrow_decimals : int
  collaterals     : [{token, address, decimals, amount_wei, liq_cf}]  — single mode
  collateral_pool : same list  — multi mode (random subset picked each time)
  pick_count      : [min, max]  — how many from pool to pick
  ltv_min / ltv_max : float  — random LTV range each open
  max_utilization : float  — skip if comet util >= this
  expiry_days     : [min, max]  — used by agent

Health:
  health = sum(collateralBalanceOf_i × price_i × liq_cf_i) / (borrowBalance × base_price)
  Close early in agent.py daily_job if health < HEALTH_CLOSE_THRESHOLD (default 1.5)
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
FACTOR_SCALE           = 10**18
REPAY_BUFFER           = 1.005    # buy 0.5% extra base token to cover accrued interest
HEALTH_CLOSE_THRESHOLD = 1.5      # close early in daily_job if health falls below this

_COMET_ABI = [
    {'name': 'supply',              'type': 'function', 'stateMutability': 'nonpayable',
     'inputs':  [{'name': 'asset',   'type': 'address'}, {'name': 'amount', 'type': 'uint256'}],
     'outputs': []},
    {'name': 'withdraw',            'type': 'function', 'stateMutability': 'nonpayable',
     'inputs':  [{'name': 'asset',   'type': 'address'}, {'name': 'amount', 'type': 'uint256'}],
     'outputs': []},
    {'name': 'borrowBalanceOf',     'type': 'function', 'stateMutability': 'view',
     'inputs':  [{'name': 'account', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'uint256'}]},
    {'name': 'balanceOf',           'type': 'function', 'stateMutability': 'view',
     'inputs':  [{'name': 'account', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'uint256'}]},
    {'name': 'collateralBalanceOf', 'type': 'function', 'stateMutability': 'view',
     'inputs':  [{'name': 'account', 'type': 'address'}, {'name': 'asset', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'uint128'}]},
    {'name': 'getUtilization',      'type': 'function', 'stateMutability': 'view',
     'inputs':  [], 'outputs': [{'name': '', 'type': 'uint256'}]},
    {'name': 'totalSupply',         'type': 'function', 'stateMutability': 'view',
     'inputs':  [], 'outputs': [{'name': '', 'type': 'uint256'}]},
    {'name': 'totalBorrow',         'type': 'function', 'stateMutability': 'view',
     'inputs':  [], 'outputs': [{'name': '', 'type': 'uint256'}]},
]


def _comet(addr: str):
    return executor.w3.eth.contract(
        address=Web3.to_checksum_address(addr), abi=_COMET_ABI
    )


# ── Encoding ───────────────────────────────────────────────────────────────────

def encode_state(collaterals: list, borrow_token: str, borrow_wei: int) -> str:
    coll_str = '|'.join(f'{c["token"]}:{c["wei"]}' for c in collaterals)
    return f'{coll_str}||{borrow_token}:{borrow_wei}'


def parse_state(encoded: str) -> tuple:
    """Returns (collaterals=[{token,wei}], borrow={token,wei})."""
    coll_part, borrow_part   = encoded.split('||')
    collaterals = []
    for item in coll_part.split('|'):
        token, wei = item.split(':')
        collaterals.append({'token': token, 'wei': int(wei)})
    borrow_token, borrow_wei = borrow_part.split(':')
    return collaterals, {'token': borrow_token, 'wei': int(borrow_wei)}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _coll_cfg(p: dict, sym: str) -> dict:
    pool = p.get('collateral_pool', p.get('collaterals', []))
    for c in pool:
        if c['token'] == sym:
            return c
    raise ValueError(f'Collateral {sym} not found in platform config')


def _borrow_price(borrow_token: str) -> float:
    if borrow_token in ('USDC', 'USDS', 'USDbC'):
        return 1.0
    return executor.get_token_usd_price(borrow_token)


def _acquire(sym: str, addr: str, amount_wei: int):
    """ETH → token. WETH=wrap, else DEX swap."""
    if DRY_RUN:
        log.info(f'[DRY RUN] acquire {sym} {amount_wei}')
        return
    if sym == 'WETH':
        _swap.wrap_eth(amount_wei)
        time.sleep(2)
    else:
        try:
            _swap.attempt_swap(_swap.swap_eth_to_token,
                               Web3.to_checksum_address(addr), amount_wei)
        except (PriceGuardError, ConfigError, SwapExecutionError) as e:
            raise RuntimeError(f'acquire {sym}: {e}')
        time.sleep(2)


def _release(sym: str, addr: str, amount_wei: int):
    """Token → ETH. Best-effort (non-fatal)."""
    if DRY_RUN:
        log.info(f'[DRY RUN] release {sym} → ETH')
        return
    try:
        if sym == 'WETH':
            _swap.unwrap_all_weth()
        else:
            _swap.attempt_swap(_swap.swap_token_to_eth,
                               Web3.to_checksum_address(addr), amount_wei)
    except Exception as e:
        log.warning(f'release {sym}→ETH failed (non-fatal): {e}')


# ── Availability ───────────────────────────────────────────────────────────────

def check_availability(comet_addr: str, max_utilization: float = 0.90) -> dict:
    c           = _comet(comet_addr)
    util_raw    = c.functions.getUtilization().call()
    utilization = util_raw / FACTOR_SCALE
    try:
        total_supply = c.functions.totalSupply().call()
        total_borrow = c.functions.totalBorrow().call()
    except Exception:
        total_supply = total_borrow = 0
    return {
        'comet':        comet_addr,
        'utilization':  utilization,
        'total_supply': total_supply,
        'total_borrow': total_borrow,
        'available':    utilization < max_utilization,
    }


# ── Health check ───────────────────────────────────────────────────────────────

def check_health(encoded: str, p: dict) -> float:
    """
    Compute current health factor for an open position.
    health = sum(on-chain collateralBalance × price × liq_cf) / (borrowBalance × base_price)
    Returns 999.0 in DRY_RUN or if no debt found.
    """
    if DRY_RUN:
        return 999.0
    try:
        collaterals, borrow_info = parse_state(encoded)
        comet    = _comet(p['comet_address'])
        debt_raw = comet.functions.borrowBalanceOf(executor.WALLET).call()
        if debt_raw == 0:
            return 999.0
        debt_usd = (debt_raw / 10**int(p.get('borrow_decimals', 6))) * _borrow_price(borrow_info['token'])

        coll_weighted = 0.0
        for c in collaterals:
            sym = c['token']
            try:
                cfg  = _coll_cfg(p, sym)
            except ValueError:
                continue
            addr    = Web3.to_checksum_address(cfg['address'])
            dec     = int(cfg['decimals'])
            liq_cf  = float(cfg['liq_cf'])
            bal_raw = comet.functions.collateralBalanceOf(executor.WALLET, addr).call()
            price   = executor.get_token_usd_price(sym)
            coll_weighted += (bal_raw / 10**dec) * price * liq_cf

        return coll_weighted / debt_usd if debt_usd > 0 else 999.0
    except Exception as e:
        log.warning(f'check_health failed: {e}')
        return 999.0


# ── Open borrow ────────────────────────────────────────────────────────────────

def open_borrow(p: dict, collateral_usd: float = 0.0) -> tuple:
    """
    Open borrow position (single or multi-collateral).

    Returns (encoded_state: str, borrow_txh: str)

    Flow:
      1. Utilization guard
      2. Pick collaterals (random subset if multi)
      3. For each: acquire token → approve → supply(comet)
      4. borrow_amount = total_coll_usd × random_ltv
      5. withdraw(base_token, borrow_amount)  ← the actual borrow TX
      6. Convert borrowed token → ETH immediately

    collateral_usd: if >0, override config amount_wei — split evenly across selected collaterals.
    """
    executor._guard()

    comet_addr   = p['comet_address']
    borrow_addr  = Web3.to_checksum_address(p['borrow_address'])
    borrow_token = p['borrow_token']
    borrow_dec   = int(p.get('borrow_decimals', 6))
    max_util     = float(p.get('max_utilization', 0.90))
    ltv_min      = float(p.get('ltv_min', 0.15))
    ltv_max      = float(p.get('ltv_max', 0.30))

    # 1. Utilization guard
    status = check_availability(comet_addr, max_util)
    log.info(
        f'compound_borrow open [{borrow_token}]: '
        f'util={status["utilization"]:.1%} available={status["available"]}'
    )
    if not status['available']:
        raise RuntimeError(
            f'BORROW_SKIP: util={status["utilization"]:.1%} >= max={max_util:.0%}'
        )
    try:
        import step_logger as _sl
        _sl.slog('check', f'util={status["utilization"]:.0%} OK')
    except Exception:
        pass

    # 2. Select collaterals
    if 'collateral_pool' in p:
        pick_min, pick_max = p.get('pick_count', [2, 3])
        pick_n      = random.randint(pick_min, min(pick_max, len(p['collateral_pool'])))
        selected    = random.sample(p['collateral_pool'], pick_n)
        log.info(f'  multi-collateral pick={pick_n}: {[c["token"] for c in selected]}')
    else:
        selected = p['collaterals']

    ltv = random.uniform(ltv_min, ltv_max)
    log.info(f'  LTV={ltv:.1%}  (range {ltv_min:.0%}-{ltv_max:.0%})')

    comet        = _comet(comet_addr)
    coll_record  = []
    total_coll_usd = 0.0

    # split collateral_usd evenly if override provided
    per_coll_usd = (collateral_usd / len(selected)) if collateral_usd > 0 else 0.0
    if per_coll_usd > 0:
        log.info(f'  collateral_usd override ${collateral_usd:.2f} → ${per_coll_usd:.2f} per collateral')

    # 3. Acquire + supply each collateral
    for coll in selected:
        sym     = coll['token']
        addr    = coll['address']
        dec     = int(coll['decimals'])
        amt_wei = int(coll['amount_wei'])
        if per_coll_usd > 0:
            price = executor.get_token_usd_price(sym)
            if price > 0:
                amt_wei = int(per_coll_usd / price * 10**dec)

        _acquire(sym, addr, amt_wei)

        executor._approve_if_needed(
            Web3.to_checksum_address(addr),
            Web3.to_checksum_address(comet_addr),
            amt_wei,
        )
        tx_s = comet.functions.supply(
            Web3.to_checksum_address(addr), amt_wei
        ).build_transaction(executor._tx_params())
        try:
            tx_s['gas'] = executor._gas_limit(tx_s)
        except Exception:
            tx_s['gas'] = 300_000
            log.warning(f'supply {sym}: estimate_gas failed — fallback 300000')
        txh_s = executor._send(tx_s)
        log.info(f'  supplied {sym} {amt_wei}  tx={txh_s}')
        try:
            import step_logger as _sl
            _sl.slog('collateral', f'{sym}  TX {txh_s[:10]}...', txhash=txh_s)
        except Exception:
            pass

        if not DRY_RUN:
            time.sleep(3)

        price           = executor.get_token_usd_price(sym)
        total_coll_usd += (amt_wei / 10**dec) * price
        coll_record.append({'token': sym, 'wei': amt_wei})

    # 4. Calculate borrow amount
    borrow_price = _borrow_price(borrow_token)
    borrow_usd   = total_coll_usd * ltv
    borrow_wei   = int(borrow_usd / borrow_price * 10**borrow_dec)

    log.info(
        f'  total_coll=${total_coll_usd:.2f}  borrow='
        f'{borrow_wei / 10**borrow_dec:.6f} {borrow_token} (${borrow_usd:.2f})'
    )

    # 5. Borrow = withdraw base token from Comet
    if not DRY_RUN:
        time.sleep(2)
    tx_b = comet.functions.withdraw(borrow_addr, borrow_wei).build_transaction(
        executor._tx_params()
    )
    try:
        tx_b['gas'] = executor._gas_limit(tx_b)
    except Exception:
        tx_b['gas'] = 300_000
        log.warning('borrow withdraw: estimate_gas failed — fallback 300000')
    borrow_txh = executor._send(tx_b)
    log.info(f'  borrowed tx={borrow_txh}')
    try:
        import step_logger as _sl
        _sl.slog('borrow', f'{borrow_token} ${borrow_usd:.2f}  TX {borrow_txh[:10]}...', txhash=borrow_txh)
    except Exception:
        pass

    # 6. Convert borrowed token → ETH immediately (keep wallet in ETH)
    if not DRY_RUN:
        time.sleep(4)
    if borrow_token == 'WETH':
        try:
            _swap.unwrap_all_weth()
        except Exception as e:
            log.warning(f'borrowed WETH unwrap failed (non-fatal): {e}')
    elif borrow_token not in ('ETH',):
        try:
            _swap.attempt_swap(_swap.swap_token_to_eth, borrow_addr, borrow_wei)
        except Exception as e:
            log.warning(f'borrowed {borrow_token}→ETH swap failed (non-fatal): {e}')
    else:
        log.info(f'[DRY RUN] convert borrowed {borrow_token} → ETH')

    encoded = encode_state(coll_record, borrow_token, borrow_wei)
    return encoded, borrow_txh


# ── Close borrow ───────────────────────────────────────────────────────────────

def close_borrow(encoded: str, p: dict) -> str:
    """
    Close borrow: repay debt + withdraw all collateral + convert everything to ETH.

    Flow:
      1. borrowBalanceOf → actual debt (includes accrued interest)
      2. Acquire base_token: actual_debt × REPAY_BUFFER (0.5% extra)
      3. supply(base_token, actual_debt) → repay
      4. For each collateral: withdraw(addr, max_uint) → get collateral back
      5. For each collateral: convert → ETH
      6. Swap surplus base_token → ETH (the 0.5% buffer leftover)

    Returns last withdraw tx hash.
    """
    executor._guard()

    collaterals, borrow_info = parse_state(encoded)
    comet_addr      = p['comet_address']
    borrow_token    = borrow_info['token']
    borrow_wei_orig = borrow_info['wei']
    borrow_dec      = int(p.get('borrow_decimals', 6))
    borrow_addr     = Web3.to_checksum_address(p['borrow_address'])

    comet = _comet(comet_addr)

    # 1. Get actual current debt
    if DRY_RUN:
        actual_debt = int(borrow_wei_orig * 1.003)
        log.info(f'[DRY RUN] debt estimate={actual_debt / 10**borrow_dec:.6f} {borrow_token}')
    else:
        actual_debt = comet.functions.borrowBalanceOf(executor.WALLET).call()
        if actual_debt == 0:
            log.warning('close_borrow: borrowBalanceOf=0 — using original amount')
            actual_debt = borrow_wei_orig
        log.info(f'close_borrow: debt={actual_debt / 10**borrow_dec:.6f} {borrow_token}')

    repay_budget = int(actual_debt * REPAY_BUFFER)

    # 2. Buy base token to repay
    if borrow_token == 'WETH':
        if DRY_RUN:
            log.info(f'[DRY RUN] wrap_eth {repay_budget} for repay')
        else:
            try:
                _swap.wrap_eth(repay_budget)
                time.sleep(3)
            except Exception as e:
                raise RuntimeError(f'close_borrow: wrap_eth for repay failed: {e}')
    else:
        try:
            _swap.attempt_swap(_swap.swap_eth_to_token, borrow_addr, repay_budget)
        except (PriceGuardError, ConfigError, SwapExecutionError) as e:
            raise RuntimeError(f'close_borrow: ETH→{borrow_token} swap failed: {e}')

    if not DRY_RUN:
        time.sleep(4)

    # 3. Repay using repay_budget (not actual_debt) — covers interest that accrues
    #    between borrowBalanceOf() call and TX mine time. Any excess becomes
    #    a tiny supply balance in Comet; we withdraw it in step 6.
    executor._approve_if_needed(borrow_addr, Web3.to_checksum_address(comet_addr), repay_budget)
    tx_repay = comet.functions.supply(borrow_addr, repay_budget).build_transaction(
        executor._tx_params()
    )
    try:
        tx_repay['gas'] = executor._gas_limit(tx_repay)
    except Exception:
        tx_repay['gas'] = 300_000
    repay_txh = executor._send(tx_repay)
    log.info(f'close_borrow: repaid tx={repay_txh}')

    if not DRY_RUN:
        time.sleep(4)

    # 4. Withdraw each collateral using actual on-chain balance.
    #    Do NOT use 2**256-1 — Comet casts amount to uint128 internally, overflow reverts.
    last_txh = repay_txh
    for c in collaterals:
        sym = c['token']
        try:
            cfg = _coll_cfg(p, sym)
        except ValueError:
            log.warning(f'close_borrow: {sym} not in config — skipping withdraw')
            continue
        coll_addr = Web3.to_checksum_address(cfg['address'])
        if DRY_RUN:
            coll_bal = c['wei']
        else:
            coll_bal = comet.functions.collateralBalanceOf(executor.WALLET, coll_addr).call()
            if coll_bal == 0:
                log.warning(f'close_borrow: collateralBalanceOf {sym}=0 — already withdrawn?')
                continue
        tx_w = comet.functions.withdraw(coll_addr, coll_bal).build_transaction(
            executor._tx_params()
        )
        try:
            tx_w['gas'] = executor._gas_limit(tx_w)
        except Exception:
            tx_w['gas'] = 300_000
        last_txh = executor._send(tx_w)
        log.info(f'close_borrow: withdrew {sym} {coll_bal} collateral tx={last_txh}')
        if not DRY_RUN:
            time.sleep(4)

    # 5. Convert each collateral → ETH
    for c in collaterals:
        sym = c['token']
        try:
            cfg = _coll_cfg(p, sym)
        except ValueError:
            continue
        coll_addr = Web3.to_checksum_address(cfg['address'])
        dec       = int(cfg['decimals'])
        if DRY_RUN:
            log.info(f'[DRY RUN] convert {sym} → ETH')
            continue
        # Read on-chain balance (withdraw max_uint gives actual amount back)
        erc20 = executor.w3.eth.contract(address=coll_addr, abi=executor.ERC20_ABI)
        bal   = erc20.functions.balanceOf(executor.WALLET).call()
        if bal == 0:
            log.warning(f'close_borrow: {sym} wallet balance=0 after withdraw (RPC lag) — using original')
            bal = c['wei']
        _release(sym, coll_addr, bal)
        if not DRY_RUN:
            time.sleep(2)

    # 6. Withdraw any Comet supply balance created by over-repaying (best-effort)
    #    repay_budget > actual debt at mine time → excess deposited as supply in Comet
    if not DRY_RUN:
        try:
            supply_bal = comet.functions.balanceOf(executor.WALLET).call()
            if supply_bal > 0:
                log.info(f'close_borrow: withdrawing supply balance {supply_bal} {borrow_token}')
                tx_ws = comet.functions.withdraw(borrow_addr, supply_bal).build_transaction(
                    executor._tx_params()
                )
                try:
                    tx_ws['gas'] = executor._gas_limit(tx_ws)
                except Exception:
                    tx_ws['gas'] = 300_000
                executor._send(tx_ws)
                time.sleep(4)
        except Exception as e:
            log.warning(f'close_borrow: withdraw supply balance failed (non-fatal): {e}')

    # 7. Swap any base_token in wallet → ETH (wallet surplus after all steps)
    if not DRY_RUN:
        try:
            if borrow_token == 'WETH':
                _swap.unwrap_all_weth()
            elif borrow_token not in ('ETH',):
                erc20   = executor.w3.eth.contract(address=borrow_addr, abi=executor.ERC20_ABI)
                surplus = erc20.functions.balanceOf(executor.WALLET).call()
                if surplus > 0:
                    _swap.attempt_swap(_swap.swap_token_to_eth, borrow_addr, surplus)
        except Exception as e:
            log.warning(f'close_borrow: surplus {borrow_token}→ETH failed (non-fatal): {e}')

    return last_txh
