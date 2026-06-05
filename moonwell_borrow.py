"""
moonwell_borrow.py — Moonwell (Compound v2 fork) borrow module, Base chain.
Comptroller: 0xfBb21d0380beE3312B33c4353c8936a0F13EF26C

Flow (single-collateral only):
  open_borrow  → supply collateral → enterMarkets → borrow → convert borrow→ETH
  close_borrow → acquire repay token → repayBorrow → redeem collateral → convert→ETH

State encoding (same format as compound_borrow):
  "COLL_TOKEN:coll_wei||BORROW_TOKEN:borrow_wei"
  e.g. "WETH:2500000000000000||USDC:875000"

Platform config keys (contracts.json):
  comptroller           : Comptroller address
  collateral_token      : symbol  (WETH / cbBTC / wstETH / EURC)
  collateral_address    : ERC20 address
  collateral_mtoken     : mToken address
  collateral_decimals   : int
  collateral_amount_wei : int   (~$5 of collateral)
  collateral_cf         : float  (for display only — health uses Comptroller on-chain)
  borrow_token          : symbol (USDC / WETH / EURC)
  borrow_address        : ERC20 address
  borrow_mtoken         : mToken address
  borrow_decimals       : int
  ltv_min / ltv_max     : float  (random per open)
  expiry_days           : [min, max]
"""

import os, time, random, logging
from web3 import Web3
from dotenv import load_dotenv
import executor
import swap as _swap
from swap import PriceGuardError, ConfigError, SwapExecutionError
import state as _state

load_dotenv()
log = logging.getLogger(__name__)

DRY_RUN                = os.getenv('DRY_RUN', '').lower() in ('1', 'true', 'yes')
REPAY_BUFFER           = 1.005
HEALTH_CLOSE_THRESHOLD = 1.5

# ── ABIs ───────────────────────────────────────────────────────────────────────

_COMPTROLLER_ABI = [
    {'name': 'enterMarkets', 'type': 'function', 'stateMutability': 'nonpayable',
     'inputs':  [{'name': 'mTokens', 'type': 'address[]'}],
     'outputs': [{'name': '', 'type': 'uint256[]'}]},
    {'name': 'getAccountLiquidity', 'type': 'function', 'stateMutability': 'view',
     'inputs':  [{'name': 'account', 'type': 'address'}],
     'outputs': [
         {'name': 'error',     'type': 'uint256'},
         {'name': 'liquidity', 'type': 'uint256'},
         {'name': 'shortfall', 'type': 'uint256'},
     ]},
]

_MTOKEN_ABI = [
    {'name': 'borrow',              'type': 'function', 'stateMutability': 'nonpayable',
     'inputs':  [{'name': 'borrowAmount', 'type': 'uint256'}],
     'outputs': [{'name': '', 'type': 'uint256'}]},
    {'name': 'repayBorrow',         'type': 'function', 'stateMutability': 'nonpayable',
     'inputs':  [{'name': 'repayAmount', 'type': 'uint256'}],
     'outputs': [{'name': '', 'type': 'uint256'}]},
    {'name': 'redeem',              'type': 'function', 'stateMutability': 'nonpayable',
     'inputs':  [{'name': 'redeemTokens', 'type': 'uint256'}],
     'outputs': [{'name': '', 'type': 'uint256'}]},
    {'name': 'balanceOf',           'type': 'function', 'stateMutability': 'view',
     'inputs':  [{'name': 'owner', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'uint256'}]},
    {'name': 'borrowBalanceStored', 'type': 'function', 'stateMutability': 'view',
     'inputs':  [{'name': 'account', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'uint256'}]},
    {'name': 'getCash',             'type': 'function', 'stateMutability': 'view',
     'inputs':  [], 'outputs': [{'name': '', 'type': 'uint256'}]},
    {'name': 'totalBorrows',        'type': 'function', 'stateMutability': 'view',
     'inputs':  [], 'outputs': [{'name': '', 'type': 'uint256'}]},
    {'name': 'totalReserves',       'type': 'function', 'stateMutability': 'view',
     'inputs':  [], 'outputs': [{'name': '', 'type': 'uint256'}]},
]

_ERC20_BAL_ABI = [
    {'name': 'balanceOf', 'type': 'function', 'stateMutability': 'view',
     'inputs':  [{'name': 'owner', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'uint256'}]},
]


def _comptroller(addr: str):
    return executor.w3.eth.contract(
        address=Web3.to_checksum_address(addr), abi=_COMPTROLLER_ABI
    )


def _mtoken(addr: str):
    return executor.w3.eth.contract(
        address=Web3.to_checksum_address(addr), abi=_MTOKEN_ABI
    )


def _token_balance(addr: str) -> int:
    c = executor.w3.eth.contract(
        address=Web3.to_checksum_address(addr), abi=_ERC20_BAL_ABI
    )
    return c.functions.balanceOf(executor.WALLET).call()


# ── State encoding ─────────────────────────────────────────────────────────────

def encode_state(coll_token: str, coll_wei: int, borrow_token: str, borrow_wei: int,
                 borrow_mtoken: str = '', borrow_addr: str = '') -> str:
    # Format: "COLL:wei||BORROW:wei:mtoken_addr:token_addr"
    # mtoken/addr appended so close_borrow uses actual borrowed mToken (handles fallback case)
    suffix = f':{borrow_mtoken}:{borrow_addr}' if borrow_mtoken else ''
    return f'{coll_token}:{coll_wei}||{borrow_token}:{borrow_wei}{suffix}'


def parse_state(encoded: str) -> tuple:
    coll_part, borrow_part = encoded.split('||')
    ct, cw = coll_part.split(':')
    borrow_fields = borrow_part.split(':')
    bt, bw = borrow_fields[0], borrow_fields[1]
    borrow_mtoken = borrow_fields[2] if len(borrow_fields) > 2 else ''
    borrow_addr   = borrow_fields[3] if len(borrow_fields) > 3 else ''
    return (
        {'token': ct, 'wei': int(cw)},
        {'token': bt, 'wei': int(bw), 'mtoken': borrow_mtoken, 'addr': borrow_addr}
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _token_price(sym: str) -> float:
    """USD price. Stables treated as 1.0 except EURC (uses Chainlink)."""
    if sym in ('USDC', 'USDS', 'USDbC', 'DAI'):
        return 1.0
    return executor.get_token_usd_price(sym)


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
    """Token → ETH. Best-effort (non-fatal). For WETH unwraps all; else swaps amount_wei."""
    if DRY_RUN:
        log.info(f'[DRY RUN] release {sym} {amount_wei} → ETH')
        return
    try:
        if sym == 'WETH':
            _swap.unwrap_all_weth()
        else:
            _swap.attempt_swap(_swap.swap_token_to_eth,
                               Web3.to_checksum_address(addr), amount_wei)
    except Exception as e:
        log.warning(f'release {sym}→ETH failed (non-fatal): {e}')


def _release_actual(sym: str, addr: str):
    """Read actual wallet balance then release. Avoids passing stale amount_wei."""
    if DRY_RUN:
        log.info(f'[DRY RUN] release_actual {sym} → ETH')
        return
    if sym == 'WETH':
        _swap.unwrap_all_weth()
        return
    bal = _token_balance(addr)
    if bal > 0:
        _release(sym, addr, bal)


# ── Availability ───────────────────────────────────────────────────────────────

_COMP_BORROW_CAP_ABI = [
    {'name': 'borrowCaps', 'type': 'function', 'stateMutability': 'view',
     'inputs':  [{'name': 'mToken', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'uint256'}]},
]

_AVAIL_CACHE: dict = {}
_AVAIL_CACHE_TTL = 30  # seconds — prevent double-check 429 within same roundtrip


def check_availability(p: dict) -> dict:
    """
    Check if borrow market has capacity.
    Returns dict with 'available' bool, 'utilization', 'cash'.
    Detects both high-util AND borrow cap exceeded (cap_raw=1 = governance-disabled).
    """
    import time as _t
    cache_key = p.get('borrow_mtoken', '')
    cached = _AVAIL_CACHE.get(cache_key)
    if cached and _t.time() - cached[1] < _AVAIL_CACHE_TTL:
        return cached[0]

    try:
        mt = _mtoken(p['borrow_mtoken'])
        cash     = mt.functions.getCash().call()
        borrows  = mt.functions.totalBorrows().call()
        reserves = mt.functions.totalReserves().call()
        total    = cash + borrows - reserves
        util     = borrows / total if total > 0 else 0

        # Check borrow cap — cap=1 is Moonwell's trick to disable borrow while keeping supply
        cap_exceeded = False
        try:
            comp = executor.w3.eth.contract(
                address=Web3.to_checksum_address(p['comptroller']),
                abi=_COMP_BORROW_CAP_ABI
            )
            cap_raw = comp.functions.borrowCaps(
                Web3.to_checksum_address(p['borrow_mtoken'])
            ).call()
            if 0 < cap_raw <= borrows:
                cap_exceeded = True
                log.info(f'borrow cap exceeded: cap={cap_raw} totalBorrows={borrows}')
        except Exception:
            pass

        borrow_wei = int(p.get('collateral_amount_wei', 0) *
                         float(p.get('ltv_max', 0.20)) *
                         _token_price(p['collateral_token']) /
                         _token_price(p['borrow_token']) /
                         (10 ** int(p['collateral_decimals'])) *
                         (10 ** int(p['borrow_decimals'])))
        result = {
            'utilization': 1.0 if cap_exceeded else util,
            'cash': cash,
            'available': (not cap_exceeded) and (cash >= borrow_wei),
            'cap_exceeded': cap_exceeded,
        }
        _AVAIL_CACHE[cache_key] = (result, _t.time())
        return result
    except Exception as e:
        log.warning(f'check_availability failed: {e}')
        return {'available': True, 'utilization': 0}  # optimistic default


# ── Health ─────────────────────────────────────────────────────────────────────

def check_health(encoded: str, p: dict) -> float:
    """
    health = (liquidity_usd + borrow_usd) / borrow_usd
    where liquidity is from Comptroller.getAccountLiquidity (scaled by 1e18).
    Returns 999.0 in DRY_RUN or if debt is zero.
    """
    if DRY_RUN:
        return 999.0
    try:
        _, borrow_info = parse_state(encoded)
        comp = _comptroller(p['comptroller'])
        err, liquidity, shortfall = comp.functions.getAccountLiquidity(executor.WALLET).call()
        if err != 0:
            log.warning(f'getAccountLiquidity error={err}')
            return 0.5
        if shortfall > 0:
            return 0.5

        mt_borrow = _mtoken(p['borrow_mtoken'])
        debt_raw  = mt_borrow.functions.borrowBalanceStored(executor.WALLET).call()
        if debt_raw == 0:
            return 999.0

        borrow_usd  = (debt_raw / 10**int(p['borrow_decimals'])) * _token_price(borrow_info['token'])
        liquidity_usd = liquidity / 1e18  # Comptroller USD value is 1e18-scaled

        return (liquidity_usd + borrow_usd) / borrow_usd if borrow_usd > 0 else 999.0
    except Exception as e:
        log.warning(f'check_health failed: {e}')
        return 999.0


# ── Open ───────────────────────────────────────────────────────────────────────

def open_borrow(p: dict, collateral_usd: float = 0.0) -> str:
    """
    1. Acquire collateral token
    2. mint mCollateral (ctoken_supply)
    3. enterMarkets → enable as collateral
    4. borrow target token
    5. Convert borrow token → ETH
    Returns encoded state string.

    collateral_usd: if >0, override config collateral_amount_wei from live price.
    """
    coll_token  = p['collateral_token']
    coll_addr   = p['collateral_address']
    coll_mtoken = p['collateral_mtoken']
    coll_dec    = int(p['collateral_decimals'])
    coll_wei    = int(p['collateral_amount_wei'])
    if collateral_usd > 0:
        _coll_price = _token_price(coll_token)
        if _coll_price > 0:
            coll_wei = int(collateral_usd / _coll_price * 10**coll_dec)
            log.info(f'  collateral_usd override ${collateral_usd:.2f} → {coll_wei/10**coll_dec:.6f} {coll_token}')

    borrow_token  = p['borrow_token']
    borrow_addr   = p['borrow_address']
    borrow_mtoken = p['borrow_mtoken']
    borrow_dec    = int(p['borrow_decimals'])

    ltv = random.uniform(float(p['ltv_min']), float(p['ltv_max']))

    # Check borrow market; switch to fallback if util too high
    # Order: config fallback → USDC → WETH (universal)
    _MW_USDC = {'token':'USDC','address':'0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
                'mtoken':'0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22','decimals':6}
    _MW_WETH = {'token':'WETH','address':'0x4200000000000000000000000000000000000006',
                'mtoken':'0x628ff693426583D9a7FB391E54366292F509D457','decimals':18}

    max_util = float(p.get('max_borrow_util', 0.95))
    avail = check_availability(p)
    if avail.get('utilization', 0) > max_util:
        candidates = []
        if p.get('borrow_fallback_token'):
            candidates.append({'token': p['borrow_fallback_token'],
                                'address': p['borrow_fallback_address'],
                                'mtoken': p['borrow_fallback_mtoken'],
                                'decimals': int(p['borrow_fallback_decimals'])})
        if borrow_token != 'USDC':
            candidates.append(_MW_USDC)
        if borrow_token != 'WETH':
            candidates.append(_MW_WETH)

        switched = False
        for fb in candidates:
            if fb['token'] == borrow_token:
                continue
            fb_avail = check_availability({**p, 'borrow_mtoken': fb['mtoken']})
            if fb_avail.get('utilization', 1.0) <= max_util:
                log.info(f'{borrow_token} util={avail["utilization"]:.1%} > {max_util:.0%} '
                         f'— switching to {fb["token"]}')
                borrow_token  = fb['token']
                borrow_addr   = fb['address']
                borrow_mtoken = fb['mtoken']
                borrow_dec    = fb['decimals']
                switched = True
                break
        if not switched:
            raise RuntimeError(f'All fallback markets also at capacity (>{max_util:.0%})')

    # 1. acquire collateral
    _acquire(coll_token, coll_addr, coll_wei)

    # 2. mint mCollateral
    executor.ctoken_supply(coll_mtoken, coll_addr, coll_wei)
    time.sleep(4)

    # 3. enterMarkets — register mCollateral as collateral
    if not DRY_RUN:
        comp = _comptroller(p['comptroller'])
        tx = comp.functions.enterMarkets(
            [Web3.to_checksum_address(coll_mtoken)]
        ).build_transaction(executor._tx_params())
        tx['gas'] = executor._gas_limit(tx)
        txh = executor._send(tx)
        log.info(f'enterMarkets tx={txh}')
        try:
            import step_logger as _sl
            _sl.slog('enter_mkt', f'TX {txh[:10]}...', txhash=txh)
        except Exception:
            pass
        time.sleep(4)
    else:
        log.info(f'[DRY RUN] SKIP enterMarkets mToken={coll_mtoken}')

    # 4. calculate borrow amount
    coll_price_usd   = _token_price(coll_token)
    coll_usd         = (coll_wei / 10**coll_dec) * coll_price_usd
    borrow_price_usd = _token_price(borrow_token)
    borrow_usd       = coll_usd * ltv
    borrow_wei       = int(borrow_usd / borrow_price_usd * 10**borrow_dec)

    log.info(f'open_borrow: coll={coll_token} ${coll_usd:.2f}  LTV={ltv:.1%}'
             f'  borrow={borrow_token} {borrow_wei} (~${borrow_usd:.2f})')

    # 5. borrow
    if not DRY_RUN:
        mt = _mtoken(borrow_mtoken)
        tx = mt.functions.borrow(borrow_wei).build_transaction(executor._tx_params())
        tx['gas'] = executor._gas_limit(tx)
        txh = executor._send(tx)
        log.info(f'borrow tx={txh}')
        try:
            import step_logger as _sl
            _sl.slog('borrow', f'{borrow_token} ${borrow_usd:.2f}  TX {txh[:10]}...', txhash=txh)
        except Exception:
            pass
        time.sleep(4)
    else:
        log.info(f'[DRY RUN] SKIP borrow {borrow_token} {borrow_wei}')

    # 6. convert borrowed token → ETH
    _release_actual(borrow_token, borrow_addr)

    return encode_state(coll_token, coll_wei, borrow_token, borrow_wei,
                        borrow_mtoken=borrow_mtoken, borrow_addr=borrow_addr)


# ── Close ──────────────────────────────────────────────────────────────────────

def close_borrow(encoded: str, p: dict, pos_id: int, dry: bool = DRY_RUN):
    """
    1. Read actual debt on-chain
    2. Acquire repay token (debt × REPAY_BUFFER)
    3. repayBorrow
    4. redeem all mCollateral shares
    5. Convert everything → ETH
    6. Close DB position
    """
    coll_info, borrow_info = parse_state(encoded)

    coll_token    = coll_info['token']
    coll_addr     = p['collateral_address']
    coll_mtoken   = p['collateral_mtoken']

    borrow_token  = borrow_info['token']
    # Use mtoken/addr from encoded state if available (handles fallback case where
    # actual borrow token differs from platform config's borrow_token)
    borrow_mtoken = borrow_info.get('mtoken') or p['borrow_mtoken']
    borrow_addr   = borrow_info.get('addr')   or p['borrow_address']
    # Resolve decimals: if token matches config use config decimals, else look up
    if borrow_token == p['borrow_token']:
        borrow_dec = int(p['borrow_decimals'])
    elif borrow_token == p.get('borrow_fallback_token'):
        borrow_dec = int(p.get('borrow_fallback_decimals', 6))
    else:
        borrow_dec = 6 if borrow_token in ('USDC', 'EURC', 'cbBTC') else 18

    # 1. read actual debt
    if not dry:
        mt_borrow   = _mtoken(borrow_mtoken)
        actual_debt = mt_borrow.functions.borrowBalanceStored(executor.WALLET).call()
        if actual_debt == 0:
            log.warning(f'borrowBalanceStored=0 for pos={pos_id} — position may already be closed')
            actual_debt = borrow_info['wei']
    else:
        actual_debt = borrow_info['wei']

    repay_wei = int(actual_debt * REPAY_BUFFER)
    log.info(f'close_borrow pos={pos_id}: repay {borrow_token} {repay_wei}  (debt={actual_debt})')

    # 2. acquire repay token
    _acquire(borrow_token, borrow_addr, repay_wei)

    # 3. approve + repayBorrow
    # Use 2**256-1 sentinel so Moonwell repays exact debt (avoids accountBorrows underflow
    # when repay_wei > actual debt due to buffer or stale borrowBalanceStored)
    REPAY_ALL = 2**256 - 1
    if not dry:
        executor._approve_if_needed(borrow_addr, borrow_mtoken, repay_wei)
        mt_borrow = _mtoken(borrow_mtoken)
        tx = mt_borrow.functions.repayBorrow(REPAY_ALL).build_transaction(executor._tx_params())
        tx['gas'] = executor._gas_limit(tx)
        txh = executor._send(tx)
        log.info(f'repayBorrow tx={txh}')
        time.sleep(4)
    else:
        log.info(f'[DRY RUN] SKIP repayBorrow {borrow_token} {repay_wei}')

    # 4. redeem all mCollateral shares
    if not dry:
        mt_coll = _mtoken(coll_mtoken)
        shares  = mt_coll.functions.balanceOf(executor.WALLET).call()
        if shares > 0:
            tx = mt_coll.functions.redeem(shares).build_transaction(executor._tx_params())
            tx['gas'] = executor._gas_limit(tx)
            txh = executor._send(tx)
            log.info(f'redeem collateral tx={txh}  shares={shares}')
            time.sleep(4)
        else:
            log.warning(f'No mCollateral shares to redeem  mToken={coll_mtoken}')
    else:
        log.info(f'[DRY RUN] SKIP redeem  mToken={coll_mtoken}')

    # 5. convert all tokens → ETH (surplus repay + collateral)
    _release_actual(borrow_token, borrow_addr)
    _release_actual(coll_token, coll_addr)

    # 6. close DB position
    if not dry:
        _state.close_position(pos_id)
        log.info(f'position {pos_id} closed')
    else:
        log.info(f'[DRY RUN] SKIP close_position {pos_id}')
