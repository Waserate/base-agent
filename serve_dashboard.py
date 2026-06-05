import os, json, time, threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()
import state
import wallet_manager as _wallet_mgr

PORT = 8766
HEALTH_CACHE_TTL  = 120   # 2 minutes
BALANCE_CACHE_TTL = 60    # 1 minute

DASHBOARD_PIN       = os.getenv('DASHBOARD_PIN',       '0000')  # action PIN (add/reroll)
DASHBOARD_ADMIN_PIN = os.getenv('DASHBOARD_ADMIN_PIN', '0000')  # emergency/withdraw PIN

_health_cache  = {'ts': 0.0, 'data': None}
_balance_cache = {'ts': 0.0, 'data': None}
_health_lock   = threading.Lock()
_balance_lock  = threading.Lock()

def _clear_all_caches():
    """Call after wallet switch to force fresh data for new wallet."""
    global _health_cache, _balance_cache
    with _health_lock:
        _health_cache  = {'ts': 0.0, 'data': None}
    with _balance_lock:
        _balance_cache = {'ts': 0.0, 'data': None}
    _LIVE_USD_CACHE.clear()
    try:
        import executor as _ex
        _ex._CFG_CACHE = None
        _ex._PRICE_CACHE.clear()
    except Exception:
        pass

# ── Add-wallet setup status (in-memory, single slot) ─────────────────────────
_setup_status = {'in_progress': False, 'wallet_id': None, 'step': '', 'error': None}
_setup_lock_obj = threading.Lock()


def _run_wallet_setup(wallet_id: str):
    """Background thread: init DB + on-chain reconcile for newly added wallet."""
    global _setup_status
    try:
        print(f'[add_wallet] switching context to {wallet_id}...')
        _setup_status['step'] = 'switching_context'
        ok, err = _wallet_mgr.switch_context(wallet_id)
        if not ok:
            raise RuntimeError(f'switch_context: {err}')

        _setup_status['step'] = 'init_db'
        print(f'[add_wallet] init DB state_{wallet_id}.db ...')
        state.init_db()

        _setup_status['step'] = 'reconciling'
        print(f'[add_wallet] on-chain recovery for {wallet_id} ...')
        import onchain_recovery
        result = onchain_recovery.reconcile(verbose=True)

        recovered = result.get('added', 0) if isinstance(result, dict) else 0
        print(f'[add_wallet] setup complete — {recovered} positions recovered')
        _setup_status['step'] = 'done'
    except Exception as e:
        print(f'[add_wallet] setup error: {e}')
        _setup_status['error'] = str(e)
        _setup_status['step']  = 'error'
    finally:
        _setup_status['in_progress'] = False


_CFG = None
def _cfg():
    global _CFG
    if _CFG is None:
        with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as f:
            _CFG = json.load(f)
    return _CFG


from name_utils import _auto_name


# Live USD estimation (on-chain price + amount_wei)
_LIVE_USD_CACHE = {}  # pid -> (usd, ts)
_LIVE_USD_TTL   = 60  # seconds

_PREVIEW_REDEEM_ABI = [
    {"name": "previewRedeem", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "shares", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]
_EXCHANGE_RATE_ABI = [
    {"name": "exchangeRateStored", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]
_SLOT0_ABI = [
    {"name": "slot0", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
                 {"name": "i0", "type": "uint16"}, {"name": "i1", "type": "uint16"},
                 {"name": "i2", "type": "uint16"}, {"name": "fP", "type": "uint32"},
                 {"name": "unlocked", "type": "bool"}]},
]
_AMM_POOL_ABI = [
    {"name": "getReserves", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "r0", "type": "uint256"}, {"name": "r1", "type": "uint256"},
                 {"name": "ts", "type": "uint256"}]},
    {"name": "totalSupply", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
]
_BEEFY_PPFS_ABI = [
    {"name": "balanceOf",            "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "getPricePerFullShare", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "want",                 "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
]
_NFPM_POS_ABI = [
    {"name": "positions", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}],
     "outputs": [
         {"name": "nonce",                    "type": "uint96"},
         {"name": "operator",                 "type": "address"},
         {"name": "token0",                   "type": "address"},
         {"name": "token1",                   "type": "address"},
         {"name": "fee",                      "type": "uint24"},
         {"name": "tickLower",                "type": "int24"},
         {"name": "tickUpper",                "type": "int24"},
         {"name": "liquidity",                "type": "uint128"},
         {"name": "feeGrowthInside0LastX128", "type": "uint256"},
         {"name": "feeGrowthInside1LastX128", "type": "uint256"},
         {"name": "tokensOwed0",              "type": "uint128"},
         {"name": "tokensOwed1",              "type": "uint128"},
     ]},
]
_UNI_NFPM  = '0x03a520b32c04bf3beef7beb72e919cf822ed34f1'
_CAKE_NFPM = '0x46A15B0b27311cedF172AB29E4f4766fbE7F4364'

_USD_DEC = {
    'USDC': 6, 'USDS': 6, 'EURC': 6, 'USDT': 6, 'sUSDS': 6, 'DOLA': 18, 'USDz': 18,
    'WETH': 18, 'wstETH': 18, 'AERO': 18, 'VIRTUAL': 18, 'cbBTC': 8,
    'MORPHO': 18, 'cbXRP': 6, 'CAKE': 18,
}


def _v3_lp_amounts(liquidity: int, tick_lower: int, tick_upper: int, sqrt_price_x96: int):
    """Uni v3 liquidity → (amount0_raw, amount1_raw). Float arithmetic OK for $5 positions."""
    import math
    Q96   = 2 ** 96
    sqA   = math.sqrt(1.0001 ** tick_lower) * Q96
    sqB   = math.sqrt(1.0001 ** tick_upper) * Q96
    sqP   = float(sqrt_price_x96)
    L     = float(liquidity)
    if sqP <= sqA:
        return L * (sqB - sqA) / (sqA * sqB) * Q96, 0.0
    elif sqP >= sqB:
        return 0.0, L * (sqB - sqA) / Q96
    else:
        return L * (sqB - sqP) / (sqP * sqB) * Q96, L * (sqP - sqA) / Q96


def _live_usd_est(pid: int, ptype: str, token: str, amount_wei, p_cfg: dict):
    """Compute live USD value from on-chain data. Returns float or None (caller falls back)."""
    now = time.time()
    cached = _LIVE_USD_CACHE.get(pid)
    if cached and now - cached[1] < _LIVE_USD_TTL:
        return cached[0]

    usd = None
    try:
        import executor
        from web3 import Web3

        # ── ERC4626 vaults (Morpho, Fluid) — previewRedeem(shares) → underlying
        if ptype == 'erc4626' and '||' not in str(amount_wei):
            vault_addr = p_cfg.get('address')
            if vault_addr:
                vault = executor.w3_read.eth.contract(
                    address=Web3.to_checksum_address(vault_addr), abi=_PREVIEW_REDEEM_ABI)
                underlying = vault.functions.previewRedeem(int(amount_wei)).call()
                dec   = _USD_DEC.get(token, 18)
                price = executor.get_token_usd_price(token)
                usd   = round(underlying / 10**dec * price, 2)

        # ── Beefy single-asset vault — getPricePerFullShare (no ERC4626) ──────
        elif ptype == 'beefy_single':
            vault_addr = p_cfg.get('address')
            if vault_addr:
                vault  = executor.w3_read.eth.contract(
                    address=Web3.to_checksum_address(vault_addr), abi=_BEEFY_PPFS_ABI)
                shares = vault.functions.balanceOf(executor.WALLET).call()
                if shares > 0:
                    ppfs          = vault.functions.getPricePerFullShare().call()
                    underlying_wei = shares * ppfs // 10**18
                    dec   = _USD_DEC.get(token, 18)
                    price = executor.get_token_usd_price(token)
                    usd   = round(underlying_wei / 10**dec * price, 2)
                else:
                    usd = 0.0

        # ── veAERO locked position ─────────────────────────────────────────────
        elif ptype == 'aero_vote':
            parts = str(amount_wei).split('|')
            if len(parts) >= 2:
                aero_wei = int(parts[1])
                usd = round(aero_wei / 1e18 * executor.get_token_usd_price('AERO'), 2)

        # ── Moonwell / Compound v2 cToken lend ────────────────────────────────
        # amount_wei inconsistency: agent stores underlying wei, recovery stores cToken shares.
        # Fix: always read on-chain balanceOf (authoritative) + exchangeRateStored.
        elif ptype == 'ctoken':
            vault_addr = p_cfg.get('address')
            if vault_addr:
                ctoken = executor.w3_read.eth.contract(
                    address=Web3.to_checksum_address(vault_addr), abi=_EXCHANGE_RATE_ABI)
                shares = ctoken.functions.balanceOf(executor.WALLET).call()
                if shares > 0:
                    rate       = ctoken.functions.exchangeRateStored().call()
                    underlying = shares * rate // (10**18)
                    dec   = _USD_DEC.get(token, 18)
                    usd   = round(underlying / 10**dec * executor.get_token_usd_price(token), 2)
                else:
                    usd = 0.0

        # ── Compound v3 / AAVE direct supply ──────────────────────────────────
        elif ptype == 'comet':
            dec = _USD_DEC.get(token, 18)
            usd = round(int(amount_wei) / 10**dec * executor.get_token_usd_price(token), 2)

        # ── Spark sUSDS ────────────────────────────────────────────────────────
        elif ptype == 'psm_hold':
            usd = round(int(amount_wei) / 1e18, 2)

        # ── Borrow positions (compound / mw / fluid / aave) ───────────────────
        elif ptype in ('compound_borrow', 'mw_borrow', 'aave_borrow', 'fluid_borrow'):
            s = str(amount_wei)
            if '||' not in s:
                pass  # unknown format → fall through
            elif s.startswith('nftId:'):
                # Fluid T1: "nftId:N||COL:sym:wei||BOR:sym:wei"
                col_usd = debt_usd = 0.0
                for part in s.split('||')[1:]:
                    tag, sym, wei_s = part.split(':', 2)
                    val = int(wei_s) / 10**_USD_DEC.get(sym, 18) * executor.get_token_usd_price(sym)
                    if tag == 'COL':
                        col_usd += val
                    elif tag == 'BOR':
                        debt_usd += val
                usd = round(col_usd - debt_usd, 2)
            else:
                # Compound/MW/AAVE: "SYM:wei|SYM:wei||BORROW_SYM:wei[:mtoken:addr]"
                col_part, debt_part = s.split('||', 1)
                col_usd = 0.0
                for piece in col_part.split('|'):
                    parts = piece.split(':')
                    if len(parts) >= 2:
                        sym, wei_s = parts[0], parts[1]
                        col_usd += int(wei_s) / 10**_USD_DEC.get(sym, 18) * executor.get_token_usd_price(sym)
                debt_parts = debt_part.split(':')
                if len(debt_parts) >= 2:
                    sym, wei_s = debt_parts[0], debt_parts[1]
                    debt_usd = int(wei_s) / 10**_USD_DEC.get(sym, 18) * executor.get_token_usd_price(sym)
                    usd = round(col_usd - debt_usd, 2)
                else:
                    usd = round(col_usd, 2)

        # ── Uniswap v3 LP (NFT tokenId) ───────────────────────────────────────
        elif ptype == 'uni_lp':
            pool_addr = p_cfg.get('pool_address')
            if pool_addr:
                token_id  = int(str(amount_wei))
                nfpm = executor.w3_read.eth.contract(
                    address=Web3.to_checksum_address(_UNI_NFPM), abi=_NFPM_POS_ABI)
                pos  = nfpm.functions.positions(token_id).call()
                liq, tick_lo, tick_hi = pos[7], pos[5], pos[6]
                if liq > 0:
                    pool = executor.w3_read.eth.contract(
                        address=Web3.to_checksum_address(pool_addr), abi=_SLOT0_ABI)
                    sqrt_p = pool.functions.slot0().call()[0]
                    a0, a1 = _v3_lp_amounts(liq, tick_lo, tick_hi, sqrt_p)
                    t0_sym = p_cfg.get('token0', 'WETH')
                    t1_sym = p_cfg.get('token1', 'USDC')
                    usd = round(
                        a0 / 10**_USD_DEC.get(t0_sym, 18) * executor.get_token_usd_price(t0_sym) +
                        a1 / 10**_USD_DEC.get(t1_sym, 18) * executor.get_token_usd_price(t1_sym),
                        2)
                else:
                    usd = 0.0

        # ── PancakeSwap v3 LP (NFT tokenId, same math as Uni v3) ──────────────
        elif ptype == 'pancake_lp':
            pool_addr = p_cfg.get('pool_address')
            if pool_addr:
                token_id = int(str(amount_wei))
                nfpm = executor.w3_read.eth.contract(
                    address=Web3.to_checksum_address(_CAKE_NFPM), abi=_NFPM_POS_ABI)
                pos  = nfpm.functions.positions(token_id).call()
                liq, tick_lo, tick_hi = pos[7], pos[5], pos[6]
                if liq > 0:
                    pool = executor.w3_read.eth.contract(
                        address=Web3.to_checksum_address(pool_addr), abi=_SLOT0_ABI)
                    sqrt_p = pool.functions.slot0().call()[0]
                    a0, a1 = _v3_lp_amounts(liq, tick_lo, tick_hi, sqrt_p)
                    t0_sym = p_cfg.get('token0', 'WETH')
                    t1_sym = p_cfg.get('token1', 'USDC')
                    usd = round(
                        a0 / 10**_USD_DEC.get(t0_sym, 18) * executor.get_token_usd_price(t0_sym) +
                        a1 / 10**_USD_DEC.get(t1_sym, 18) * executor.get_token_usd_price(t1_sym),
                        2)
                else:
                    usd = 0.0

        # ── AAVE v3 supply (amount_wei = original underlying amount) ─────────
        elif ptype == 'aave_supply':
            dec   = _USD_DEC.get(token, 18)
            price = executor.get_token_usd_price(token)
            usd   = round(int(amount_wei) / 10**dec * price, 2)

        # ── Beefy LP vault (Aerodrome LP wrapped) ─────────────────────────────
        elif ptype == 'beefy_lp':
            vault_addr = p_cfg.get('address')
            lp_addr    = p_cfg.get('lp_address')
            if vault_addr and lp_addr:
                vault  = executor.w3_read.eth.contract(
                    address=Web3.to_checksum_address(vault_addr), abi=_BEEFY_PPFS_ABI)
                shares = vault.functions.balanceOf(executor.WALLET).call()
                if shares > 0:
                    ppfs     = vault.functions.getPricePerFullShare().call()
                    lp_amt   = shares * ppfs // 10**18
                    pool     = executor.w3_read.eth.contract(
                        address=Web3.to_checksum_address(lp_addr), abi=_AMM_POOL_ABI)
                    r0, r1, _ = pool.functions.getReserves().call()
                    total_sup = pool.functions.totalSupply().call()
                    cfg_t0 = p_cfg.get('token0_address', '')
                    cfg_t1 = p_cfg.get('token1_address', '')
                    if cfg_t0 and cfg_t1 and cfg_t0.lower() > cfg_t1.lower():
                        t0_sym, t1_sym = p_cfg.get('token1', ''), p_cfg.get('token0', '')
                    else:
                        t0_sym, t1_sym = p_cfg.get('token0', ''), p_cfg.get('token1', '')
                    if total_sup > 0:
                        share = lp_amt / total_sup
                        usd = round(
                            share * r0 / 10**_USD_DEC.get(t0_sym, 18) * executor.get_token_usd_price(t0_sym) +
                            share * r1 / 10**_USD_DEC.get(t1_sym, 18) * executor.get_token_usd_price(t1_sym),
                            2)
                else:
                    usd = 0.0

        # ── Aerodrome AMM LP (LP token amount, staked in gauge) ───────────────
        elif ptype == 'aero_lp':
            pool_addr = p_cfg.get('pool_address') or p_cfg.get('address')
            if pool_addr:
                pool = executor.w3_read.eth.contract(
                    address=Web3.to_checksum_address(pool_addr), abi=_AMM_POOL_ABI)
                r0, r1, _ = pool.functions.getReserves().call()
                total_sup  = pool.functions.totalSupply().call()
                lp_bal     = int(str(amount_wei))
                if total_sup > 0 and lp_bal > 0:
                    # Aerodrome sorts tokens by address — may differ from config order
                    cfg_t0_addr = p_cfg.get('token0_address', '')
                    cfg_t1_addr = p_cfg.get('token1_address', '')
                    if cfg_t0_addr and cfg_t1_addr and cfg_t0_addr.lower() > cfg_t1_addr.lower():
                        # config token0/token1 are swapped vs actual pool
                        t0_sym, t1_sym = p_cfg.get('token1', 'AERO'), p_cfg.get('token0', 'WETH')
                    else:
                        t0_sym, t1_sym = p_cfg.get('token0', 'WETH'), p_cfg.get('token1', 'AERO')
                    share  = lp_bal / total_sup
                    usd = round(
                        share * r0 / 10**_USD_DEC.get(t0_sym, 18) * executor.get_token_usd_price(t0_sym) +
                        share * r1 / 10**_USD_DEC.get(t1_sym, 18) * executor.get_token_usd_price(t1_sym),
                        2)
                else:
                    usd = 0.0

    except Exception:
        pass

    if usd is not None:
        _LIVE_USD_CACHE[pid] = (usd, now)
    return usd


def build_state():
    state.init_db()
    today = date.today()
    active = []
    for pos in state.get_active():
        pid, platform, token, amount_wei = pos[0], pos[1], pos[2], pos[3]
        entry_date, expiry_date, tx_hash = pos[4], pos[5], pos[6]
        days_left = (date.fromisoformat(expiry_date) - today).days

        # Determine position type from config
        p_cfg = _cfg().get('platforms', {}).get(platform, {})
        ptype = p_cfg.get('type', 'lend')

        # Classify display type
        borrow_types = {'compound_borrow', 'mw_borrow', 'fluid_borrow', 'aave_borrow'}
        lp_types     = {'aero_lp', 'uni_lp', 'pancake_lp', 'beefy_lp'}
        if ptype in borrow_types:
            display_type = 'borrow'
        elif ptype in lp_types or 'lp' in ptype:
            display_type = 'lp'
        elif ptype == 'aero_vote':
            display_type = 'vote'
        else:
            display_type = 'lend'

        # Amount display
        try:
            if '||' in str(amount_wei):
                amount_display = 'encoded'
            else:
                dec_map = {'USDC': 6, 'USDS': 6, 'EURC': 6,
                           'WETH': 18, 'wstETH': 18, 'AERO': 18,
                           'cbBTC': 8, 'sUSDS': 6}
                # ERC4626 vaults always issue 18-dec shares regardless of underlying token
                if ptype == 'erc4626':
                    dec = 18
                else:
                    dec = dec_map.get(token, 18)
                amount_display = f'{round(int(amount_wei) / 10**dec, 6)}'
        except Exception:
            amount_display = str(amount_wei)[:20]

        # opened_usd: column index 8 (added via migrate_db)
        opened_usd = pos[8] if len(pos) > 8 and pos[8] is not None else None

        # Live on-chain price (Option A): overrides static opened_usd
        live = _live_usd_est(pid, ptype, token, amount_wei, p_cfg)
        if live is not None:
            usd_est = live
        elif opened_usd is not None:
            usd_est = round(float(opened_usd), 2)
        elif ptype == 'erc4626' and '||' not in str(amount_wei):
            # Recovery-restored dust: shares < 0.01 → $0
            try:
                shares = int(amount_wei) / 1e18
                usd_est = 0.0 if shares < 0.01 else 5.0
            except Exception:
                usd_est = 5.0
        else:
            usd_est = 5.0

        import rule_engine as _re
        _lp_types = {'aero_lp', 'uni_lp', 'pancake_lp', 'beefy_lp'}
        active.append({
            'id':               pid,
            'platform':         platform,
            'display_name':     _auto_name(platform),
            'protocol':         _re.get_protocol(platform, p_cfg),
            'token':            token,
            'token0':           p_cfg.get('token0', '') if ptype in _lp_types else '',
            'token1':           p_cfg.get('token1', '') if ptype in _lp_types else '',
            'amount_display':   amount_display,
            'usd_est':          usd_est,
            'type':             display_type,
            'ptype':            ptype,
            'entry_date':       entry_date,
            'expiry_date':      expiry_date,
            'days_left':        days_left,
            'tx_hash':          tx_hash or '',
            'collateral_token': p_cfg.get('collateral_token', '') if ptype in ('aave_borrow', 'mw_borrow') else '',
            'locked_by_borrow': False,
        })

    # Post-process: when a standalone supply (aave_supply / Moonwell ctoken) shares the same
    # token as a borrow's collateral, the on-chain aToken/mToken balance is shared.
    # Zero out the supply's USD (already captured in borrow's encoded-state net) and flag it.
    _SUPPLY_LOCK_TYPES = {'aave_supply', 'ctoken'}
    _BORROW_COLL_TYPES = {'aave_borrow', 'mw_borrow'}
    _borrow_by_col = {}  # collateral_token -> list index in active
    for i, pos in enumerate(active):
        if pos['ptype'] in _BORROW_COLL_TYPES and pos['collateral_token']:
            _borrow_by_col.setdefault(pos['collateral_token'], []).append(i)
    for pos in active:
        if pos['ptype'] in _SUPPLY_LOCK_TYPES and pos['token'] in _borrow_by_col:
            pos['usd_est']        = 0.0
            pos['locked_by_borrow'] = True

    _BORROW_PT = {'compound_borrow', 'mw_borrow', 'fluid_borrow', 'aave_borrow'}
    _LP_PT     = {'aero_lp', 'beefy_lp', 'uni_lp', 'pancake_lp', 'beefy_single'}
    cat_counts = {'lp': 0, 'lend': 0, 'borrow': 0}
    for pos in active:
        if (pos.get('usd_est') or 0.0) < 1.0:
            continue  # dust position - doesn't hold a slot
        pt = pos.get('ptype', '')
        if pt in _BORROW_PT:
            cat_counts['borrow'] += 1
        elif pt in _LP_PT:
            cat_counts['lp'] += 1
        elif pt not in ('aero_vote',):
            cat_counts['lend'] += 1
    try:
        import settings as _settings
        max_conc = _settings.load().get('max_concurrent', {'lp': 5, 'lend': 6, 'borrow': 4})
    except Exception:
        max_conc = {'lp': 5, 'lend': 6, 'borrow': 4}

    from collections import defaultdict
    _proto_agg = defaultdict(lambda: {'count': 0, 'usd': 0.0})
    for pos in active:
        _pk = pos['protocol']
        if _pk == 'aav':
            _pk = 'aave'   # normalize: aav_borrow platforms → same AAVE key
        _proto_agg[_pk]['count'] += 1
        _proto_agg[_pk]['usd']   += pos.get('usd_est', 0.0)
    protocol_summary = {k: {'count': v['count'], 'usd': round(v['usd'], 2)} for k, v in _proto_agg.items()}

    return {
        'wallet':            os.getenv('WALLET_ADDRESS', ''),
        'active_positions':  active,
        'active_count':      len(active),
        'category_counts':   cat_counts,
        'max_concurrent':    max_conc,
        'protocol_summary':  protocol_summary,
        'generated_at':      today.isoformat(),
    }


def build_all_state() -> dict:
    """
    Aggregate active positions from ALL wallets with live USD estimates.
    Switches context per wallet and calls build_state() to reuse all live USD logic.
    Injects wallet_id + wallet_name into each position row.
    Returns protocol_summary aggregated across all wallets.
    """
    import importlib as _il, sys as _sys
    today        = date.today()
    all_wallets  = _wallet_mgr.load_wallets()
    original_wid = os.environ.get('WALLET_ID', 'default')
    combined     = []

    for w in all_wallets:
        wid   = w['id']
        wname = w.get('name', wid)
        try:
            ok, err = _wallet_mgr.switch_context(wid)
            if not ok:
                continue
            for _m in ('executor', 'state'):
                if _m in _sys.modules:
                    _il.reload(_sys.modules[_m])
            # Clear live USD cache so prices are fetched fresh for this wallet
            _LIVE_USD_CACHE.clear()

            wallet_state = build_state()
            for pos in (wallet_state.get('active_positions') or []):
                pos['wallet_id']   = wid
                pos['wallet_name'] = wname
                combined.append(pos)
        except Exception:
            continue

    # Restore original context
    try:
        _wallet_mgr.switch_context(original_wid)
        for _m in ('executor', 'state'):
            if _m in _sys.modules:
                _il.reload(_sys.modules[_m])
    except Exception:
        pass
    _clear_all_caches()

    # Aggregate protocol_summary across all wallets
    proto_summary: dict = {}
    for p in combined:
        proto = p.get('protocol') or ''
        if proto == 'aav':
            proto = 'aave'
        if proto not in proto_summary:
            proto_summary[proto] = {'usd': 0.0, 'count': 0}
        proto_summary[proto]['usd']   = round(proto_summary[proto]['usd'] + (p.get('usd_est') or 0), 2)
        proto_summary[proto]['count'] += 1

    total_usd = sum(p.get('usd_est') or 0 for p in combined)
    return {
        'wallet':           'ALL',
        'active_positions': combined,
        'active_count':     len(combined),
        'total_usd':        round(total_usd, 2),
        'protocol_summary': proto_summary,
        'generated_at':     today.isoformat(),
    }


def do_reconcile_all() -> dict:
    """Run onchain_recovery.reconcile() for ALL wallets. Returns per-wallet results."""
    import importlib as _il, sys as _sys
    original_wid = os.environ.get('WALLET_ID', 'default')
    results: dict = {}

    for w in _wallet_mgr.load_wallets():
        wid = w['id']
        try:
            _wallet_mgr.switch_context(wid)
            for _m in ('executor', 'state', 'onchain_recovery'):
                if _m in _sys.modules:
                    _il.reload(_sys.modules[_m])
            import state as _st
            _st.init_db()
            import onchain_recovery as _ocr
            r = _ocr.reconcile(verbose=False)
            results[wid] = r
        except Exception as e:
            results[wid] = {'error': str(e)}

    # Restore original context + clear caches
    try:
        _wallet_mgr.switch_context(original_wid)
        for _m in ('executor', 'state'):
            if _m in _sys.modules:
                _il.reload(_sys.modules[_m])
    except Exception:
        pass
    _clear_all_caches()
    return {'ok': True, 'results': results}


def _with_wallet_context(wid: str, fn):
    """Switch to wallet wid, run fn(), restore original context. Returns fn() result."""
    import importlib as _il, sys as _sys
    original_wid = os.environ.get('WALLET_ID', 'default')
    try:
        ok, err = _wallet_mgr.switch_context(wid)
        if not ok:
            return {'error': f'Cannot switch to wallet {wid}: {err}'}
        for _m in ('executor', 'state'):
            if _m in _sys.modules:
                _il.reload(_sys.modules[_m])
        return fn()
    except Exception as e:
        return {'error': str(e)}
    finally:
        try:
            _wallet_mgr.switch_context(original_wid)
            for _m in ('executor', 'state'):
                if _m in _sys.modules:
                    _il.reload(_sys.modules[_m])
        except Exception:
            pass
        _clear_all_caches()


def do_plan_all_add() -> dict:
    """Add one action to EVERY wallet's plan. Returns per-wallet results."""
    results = {}
    for w in _wallet_mgr.load_wallets():
        wid = w['id']
        results[wid] = _with_wallet_context(wid, do_add_to_plan)
    ok_count = sum(1 for r in results.values() if r.get('ok'))
    return {'ok': True, 'results': results,
            'message': f'Added to {ok_count}/{len(results)} wallets'}


def do_plan_all_reroll() -> dict:
    """Reroll all pending actions for EVERY wallet. Returns per-wallet results."""
    import importlib as _il, sys as _sys
    all_results = {}

    for w in _wallet_mgr.load_wallets():
        wid = w['id']
        def _reroll_wallet():
            from daily_briefing import load_plan
            plan = load_plan()
            if not plan:
                return {'ok': False, 'error': 'No plan today', 'results': []}
            pending = [a for a in plan if not a.get('done', False)]
            if not pending:
                return {'ok': True, 'results': [], 'message': 'All done — nothing to reroll'}
            results = []
            for action in pending:
                res = do_reroll(action['idx'])
                results.append({'idx': action['idx'], 'ok': res.get('ok', False),
                                'new_platform': res.get('new_platform'),
                                'error': res.get('error')})
            ok_count = sum(1 for r in results if r['ok'])
            return {'ok': True, 'results': results,
                    'message': f'Rerolled {ok_count}/{len(results)} actions'}
        all_results[wid] = _with_wallet_context(wid, _reroll_wallet)

    return {'ok': True, 'results': all_results}


def build_all_plan() -> dict:
    """
    Aggregate today's plan from ALL wallet plan files (plan_{wid}.json).
    Injects wallet_id + wallet_name into each action.
    Actions from multiple wallets are merged, sorted by time_bkk.
    """
    cache_dir   = os.path.join(os.path.dirname(__file__), 'cache')
    today       = date.today().isoformat()
    all_wallets = _wallet_mgr.load_wallets()
    combined    = []

    for w in all_wallets:
        wid   = w['id']
        wname = w.get('name', wid)
        path  = os.path.join(cache_dir, f'plan_{wid}.json')
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get('date') != today:
                continue
            for a in (data.get('actions') or []):
                action = dict(a)
                action['wallet_id']   = wid
                action['wallet_name'] = wname
                combined.append(action)
        except FileNotFoundError:
            continue
        except Exception:
            continue

    combined.sort(key=lambda x: x.get('time_bkk', '00:00'))
    return {'date': today, 'actions': combined}


def build_history(limit=30):
    state.init_db()
    cfg = _cfg()
    rows = []
    for pos in state.all_positions():
        pid, platform, token, amount_wei, status = pos[0], pos[1], pos[2], pos[3], pos[7] if len(pos) > 7 else 'unknown'
        entry_date, expiry_date, tx_hash = pos[4], pos[5], pos[6]
        if status != 'closed':
            continue
        p_cfg = cfg.get('platforms', {}).get(platform, {})
        ptype = p_cfg.get('type', 'lend')
        borrow_types = {'compound_borrow', 'mw_borrow', 'fluid_borrow', 'aave_borrow'}
        lp_types     = {'aero_lp', 'uni_lp', 'pancake_lp', 'beefy_lp'}
        if ptype in borrow_types:
            display_type = 'borrow'
        elif ptype in lp_types or 'lp' in ptype:
            display_type = 'lp'
        else:
            display_type = 'lend'
        opened_usd = pos[8] if len(pos) > 8 and pos[8] is not None else None
        closed_usd = pos[9] if len(pos) > 9 and pos[9] is not None else None
        usd_est = round(float(closed_usd), 2) if closed_usd is not None else (round(float(opened_usd), 2) if opened_usd is not None else 5.0)
        rows.append({
            'id':           pid,
            'platform':     platform,
            'display_name': _auto_name(platform),
            'token':        token,
            'type':         display_type,
            'entry_date':   entry_date,
            'usd_est':      usd_est,
            'tx_hash':      tx_hash or '',
        })
        if len(rows) >= limit:
            break
    return {'rows': rows, 'total': len(rows)}


def build_health(force: bool = False):
    now = time.time()
    with _health_lock:
        if not force and now - _health_cache['ts'] < HEALTH_CACHE_TTL and _health_cache['data'] is not None:
            # Skip cache if all results are 999.0 (likely stale from unsettled borrow)
            cached = _health_cache['data']
            all_999 = cached.get('results') and all(
                r.get('health', 0) >= 999.0 for r in cached['results']
            )
            if not all_999:
                data = dict(cached)
                data['from_cache'] = True
                return data

    try:
        from health_monitor import check_all
        results = check_all()
    except Exception as e:
        results = [{'error': str(e), 'status': 'ERROR'}]

    for r in results:
        if 'platform' in r:
            r['display_platform'] = _auto_name(r['platform'])
    data = {
        'results':    results,
        'checked_at': date.today().isoformat(),
        'from_cache': False,
        'cache_ttl':  HEALTH_CACHE_TTL,
    }
    with _health_lock:
        _health_cache['ts']   = time.time()
        _health_cache['data'] = data
    return data


def build_all_health() -> dict:
    """Collect health data from ALL wallets. Returns per-wallet health results."""
    wallets = _wallet_mgr.load_wallets()
    results_by_wallet = {}
    for w in wallets:
        wid   = w['id']
        wname = w.get('name', wid)
        try:
            health_data = _with_wallet_context(wid, lambda: build_health())
            results_by_wallet[wid] = {
                'wallet_name': wname,
                'results':     health_data.get('results', []),
                'from_cache':  health_data.get('from_cache', False),
            }
        except Exception as e:
            results_by_wallet[wid] = {'wallet_name': wname, 'results': [], 'error': str(e)}
    return {'wallets': results_by_wallet}


def _count_closed_by_type():
    cfg = _cfg()
    cats = {'LP': 0, 'LEND': 0, 'BORROW': 0, 'VOTE': 0,
            'SPARK': 0, 'GAME': 0, 'DEPLOY': 0, 'AVANTIS': 0}
    lp_types     = {'aero_lp', 'uni_lp', 'pancake_lp', 'beefy_lp'}
    borrow_types = {'compound_borrow', 'mw_borrow', 'fluid_borrow', 'aave_borrow'}
    for pos in state.all_positions():
        status   = pos[7] if len(pos) > 7 else ''
        if status != 'closed':
            continue
        platform = pos[1]
        ptype    = cfg.get('platforms', {}).get(platform, {}).get('type', 'lend')
        if platform == 'spark_susds':
            cats['SPARK'] += 1
        elif platform == 'megapot':
            cats['GAME'] += 1
        elif platform == 'deploy_contract':
            cats['DEPLOY'] += 1
        elif platform == 'aero_vote':
            cats['VOTE'] += 1
        elif platform.startswith('avantis'):
            cats['AVANTIS'] += 1
        elif ptype in lp_types or 'lp' in ptype:
            cats['LP'] += 1
        elif ptype in borrow_types:
            cats['BORROW'] += 1
        else:
            cats['LEND'] += 1
    return cats


def build_stats():
    state.init_db()
    rows = state.get_daily_stats(30)
    cutoff = (date.today() - timedelta(days=29)).isoformat()
    pos_totals = state.compute_positions_totals(cutoff)
    return {
        'daily':          rows,
        'type_counts':    _count_closed_by_type(),
        'positions_gas':  pos_totals['gas_usd'],
        'positions_vol':  pos_totals['volume_usd'],
        'generated_at':   date.today().isoformat(),
    }


_CACHE_DIR = os.path.join(os.path.dirname(__file__), 'cache')

def _get_rule_log_file() -> str:
    wid = os.environ.get('WALLET_ID', 'default')
    return os.path.join(_CACHE_DIR, f'rule_log_{wid}.json')

def _get_action_log_file() -> str:
    wid = os.environ.get('WALLET_ID', 'default')
    return os.path.join(_CACHE_DIR, f'action_log_{wid}.json')

def build_rule_log():
    try:
        with open(_get_rule_log_file()) as f:
            entries = json.load(f)
        for e in entries:
            if 'current' in e:
                e['display_current']  = _auto_name(e['current'])
            if 'original' in e:
                e['display_original'] = _auto_name(e['original'])
        return {'entries': list(reversed(entries)), 'count': len(entries)}
    except FileNotFoundError:
        return {'entries': [], 'count': 0}
    except Exception as e:
        return {'entries': [], 'count': 0, 'error': str(e)}


def do_add_to_plan() -> dict:
    """
    Add one new action to today's plan (scheduled, not executed immediately).
    Picks a valid candidate platform via rule_engine, assigns a random future BKK time.
    plan_sync_job will schedule it within 60s.
    """
    from datetime import datetime as _dt, timedelta as _td, time as _dtime
    import random as _random

    try:
        from daily_briefing import load_plan, get_plan_file
        import rule_engine, executor
        import state as _state

        today = date.today()
        plan  = load_plan()
        if plan is None:
            plan = []

        # Exclude: already active platforms + platforms in plan (done or not)
        _state.init_db()
        active     = _state.get_active()
        active_set = {p[1] for p in active}
        plan_protos = set()
        for a in plan:
            p_cfg = _cfg().get('platforms', {}).get(a['platform'], {})
            plan_protos.add(rule_engine.get_protocol(a['platform'], p_cfg))

        all_p = [k for k, v in _cfg().get('platforms', {}).items()
                 if isinstance(v, dict) and v.get('type') not in ('aero_vote',)]
        candidates = rule_engine.filter_candidates(
            all_p, active_set, plan_protos, _cfg()['platforms'], active, []
        )
        _random.shuffle(candidates)

        eth = executor.get_eth_balance()
        today_opened = plan_protos.copy()

        new_platform = None
        for attempt, pk in enumerate(candidates[:15], 1):
            ok, reason = rule_engine.pre_action_validate(
                pk, _cfg()['platforms'], active, [], today_opened, eth
            )
            if ok:
                new_platform = pk
                break

        if new_platform is None:
            return {'error': 'No valid platform found after 15 attempts (rule engine blocked all)'}

        # Pick random time between now+5min and 23:50 BKK
        now_bkk = _dt.utcnow() + _td(hours=7)
        earliest = now_bkk.hour * 60 + now_bkk.minute + 5
        latest   = 23 * 60 + 50
        if earliest >= latest:
            return {'error': 'Too late to add to plan today (past 23:45 BKK)'}

        used_times = {int(a['time_bkk'].replace(':', '')) for a in plan}
        for _ in range(20):
            mins = _random.randint(earliest, latest)
            t_int = (mins // 60) * 100 + (mins % 60)
            if t_int not in used_times:
                break

        h, m    = divmod(mins, 60)
        time_bkk = f'{h:02d}:{m:02d}'
        bkk_dt   = _dt.combine(today, _dtime(h, m))
        utc_dt   = bkk_dt - _td(hours=7)

        p_cfg     = _cfg()['platforms'].get(new_platform, {})
        ptype     = p_cfg.get('type', '')
        disp_type = 'BORROW' if 'borrow' in ptype else 'LP' if 'lp' in ptype else 'LEND'
        token     = p_cfg.get('token') or p_cfg.get('borrow_token', '')
        try:
            import rule_engine as _re_local
            protocol = _re_local.get_protocol(new_platform, p_cfg)
        except Exception:
            protocol = new_platform.split('_')[0]

        try:
            usd_est = rule_engine.pick_amount_usd()
        except Exception:
            usd_est = 5.0

        try:
            import settings as _settings
            expiry_days = _settings.expiry_for_type(ptype)
        except Exception:
            import random as _rand
            days_cfg    = p_cfg.get('expiry_days', [3, 5])
            expiry_days = _rand.randint(int(days_cfg[0]), int(days_cfg[1]))

        next_idx = max((a['idx'] for a in plan), default=0) + 1
        new_action = {
            'idx':          next_idx,
            'platform':     new_platform,
            'display_name': _auto_name(new_platform),
            'protocol':     protocol,
            'type':         ptype,
            'disp_type':    disp_type,
            'token':        token,
            'usd_est':      usd_est,
            'expiry_days':  expiry_days,
            'time_bkk':     time_bkk,
            'run_at_utc':   utc_dt.isoformat(),
            'date':         today.isoformat(),
            'done':         False,
        }
        plan.append(new_action)
        plan.sort(key=lambda x: x['time_bkk'])

        import json as _json
        with open(get_plan_file(), 'w') as f:
            _json.dump({'date': today.isoformat(), 'actions': plan}, f, indent=2)

        return {
            'ok':           True,
            'platform':     new_platform,
            'display_name': _auto_name(new_platform),
            'time_bkk':     time_bkk,
            'usd_est':      usd_est,
            'message':      f'Added {new_platform} to plan at {time_bkk} BKK — agent will schedule within 60s',
        }

    except Exception as e:
        return {'error': str(e)}


def build_action_log():
    try:
        with open(_get_action_log_file()) as f:
            entries = json.load(f)
        return {'entries': list(reversed(entries)), 'count': len(entries)}
    except FileNotFoundError:
        return {'entries': [], 'count': 0}
    except Exception as e:
        return {'entries': [], 'count': 0, 'error': str(e)}


def do_reroll(idx: int) -> dict:
    """
    Reroll a TODO action by idx (1-based).
    - If no actions done yet: can reroll any idx
    - If some done: can only reroll done=False actions
    Returns updated plan or error dict.
    """
    from datetime import datetime as _dt, timedelta as _td, time as _dtime
    import random as _random

    try:
        from daily_briefing import load_plan, get_plan_file
        import rule_engine, executor

        plan = load_plan()
        if not plan:
            return {'error': 'No plan for today'}

        # Find target action
        target = next((a for a in plan if a['idx'] == idx), None)
        if target is None:
            return {'error': f'Action #{idx} not found'}
        if target.get('done', False):
            return {'error': f'Action #{idx} already done — cannot reroll'}

        # Build exclude set: platforms already active or done today
        import state as _state
        _state.init_db()
        active      = _state.get_active()
        active_set  = {p[1] for p in active}
        done_protos = set()
        for a in plan:
            if a.get('done', False):
                p_cfg = _cfg().get('platforms', {}).get(a['platform'], {})
                done_protos.add(rule_engine.get_protocol(a['platform'], p_cfg))

        # Pick replacement (not same protocol as done actions or other TODO actions this reroll)
        other_todo_protos = set()
        for a in plan:
            if a['idx'] != idx and not a.get('done', False):
                p_cfg = _cfg().get('platforms', {}).get(a['platform'], {})
                other_todo_protos.add(rule_engine.get_protocol(a['platform'], p_cfg))

        all_p = [k for k, v in _cfg().get('platforms', {}).items()
                 if isinstance(v, dict) and v.get('type') not in ('aero_vote',)]
        candidates = rule_engine.filter_candidates(
            all_p, active_set, done_protos | other_todo_protos,
            _cfg()['platforms'], active, []
        )
        _random.shuffle(candidates)

        eth = executor.get_eth_balance()
        today_opened = done_protos | other_todo_protos

        new_platform = None
        for attempt, pk in enumerate(candidates[:10], 1):
            ok, reason = rule_engine.pre_action_validate(
                pk, _cfg()['platforms'], active, [], today_opened, eth
            )
            _write_rule_log('reroll', pk, attempt, ok, reason,
                            outcome='selected' if ok else None, context='reroll')
            if ok:
                new_platform = pk
                break

        if new_platform is None:
            return {'error': 'No valid replacement found after 10 attempts'}

        # Pick new time: now+5min to 23:50 BKK, not conflicting with other TODO times
        now_bkk_mins = (_dt.utcnow() + _td(hours=7)).hour * 60 + (_dt.utcnow() + _td(hours=7)).minute
        earliest = max(now_bkk_mins + 5, 7*60+1)
        latest   = 23*60+50
        if earliest >= latest:
            return {'error': 'Too late to reroll today (past 23:45 BKK)'}

        used_times = {
            int(a['time_bkk'].replace(':',''))
            for a in plan if a['idx'] != idx and not a.get('done', False)
        }
        for _ in range(20):
            mins = _random.randint(earliest, latest)
            if mins not in used_times:
                break

        h, m = divmod(mins, 60)
        time_bkk  = f'{h:02d}:{m:02d}'
        today     = date.today()
        bkk_dt    = _dt.combine(today, _dtime(h, m))
        utc_dt    = bkk_dt - _td(hours=7)

        # Update plan
        p_cfg = _cfg()['platforms'].get(new_platform, {})
        ptype = p_cfg.get('type', '')
        disp_type = 'BORROW' if 'borrow' in ptype else 'LP' if 'lp' in ptype else 'LEND'
        token = p_cfg.get('token') or p_cfg.get('borrow_token', '')
        try:
            protocol = rule_engine.get_protocol(new_platform, p_cfg)
        except Exception:
            protocol = new_platform.split('_')[0]

        try:
            usd_est = rule_engine.pick_amount_usd()
        except Exception:
            usd_est = 5.0

        try:
            import settings as _settings
            expiry_days = _settings.expiry_for_type(ptype)
        except Exception:
            import random as _rand
            days_cfg    = p_cfg.get('expiry_days', [3, 5])
            expiry_days = _rand.randint(int(days_cfg[0]), int(days_cfg[1]))

        target.update({
            'platform':     new_platform,
            'display_name': _auto_name(new_platform),
            'protocol':     protocol,
            'type':         ptype,
            'disp_type':    disp_type,
            'token':        token,
            'usd_est':      usd_est,
            'expiry_days':  expiry_days,
            'time_bkk':     time_bkk,
            'run_at_utc':   utc_dt.isoformat(),
            'done':         False,
        })

        import json as _json
        with open(get_plan_file(), 'w') as f:
            _json.dump({'date': today.isoformat(), 'actions': plan}, f, indent=2)

        return {'ok': True, 'plan': plan, 'new_platform': new_platform, 'new_time': time_bkk}

    except Exception as e:
        return {'error': str(e)}


def _write_rule_log(original, current, attempt, ok, reason, outcome=None, context='reroll'):
    from datetime import datetime as _dt
    entry = {
        'ts': _dt.now().strftime('%H:%M:%S'), 'date': date.today().isoformat(),
        'context': context, 'original': original, 'current': current,
        'attempt': attempt, 'ok': ok, 'reason': reason, 'outcome': outcome,
    }
    try:
        log_file = _get_rule_log_file()
        try:
            with open(log_file) as f:
                entries = json.load(f)
        except Exception:
            entries = []
        entries.append(entry)
        with open(log_file, 'w') as f:
            json.dump(entries[-100:], f, indent=2)
    except Exception:
        pass


def build_briefing():
    try:
        from daily_briefing import build
        data = build()
        # Append manual-withdraw alerts from flag files (written when auto-retry exhausted)
        wid = os.environ.get('WALLET_ID', 'default')
        _flag = os.path.join(_CACHE_DIR, f'manual_withdraw_{wid}.json')
        if os.path.exists(_flag):
            try:
                import json as _jf
                items = _jf.load(open(_flag))
                for item in items:
                    msg = (f"MANUAL WITHDRAW NEEDED: {item['platform']} "
                           f"(pos#{item['pos_id']}, expired {item['expiry']}) — กด WITHDRAW ใน Active Positions")
                    data.setdefault('warnings', [])
                    if msg not in data['warnings']:
                        data['warnings'].append(msg)
            except Exception:
                pass
        return data
    except Exception as e:
        return {'error': str(e)}


def build_balance():
    now = time.time()
    with _balance_lock:
        if now - _balance_cache['ts'] < BALANCE_CACHE_TTL and _balance_cache['data'] is not None:
            data = dict(_balance_cache['data'])
            data['from_cache'] = True
            return data

    try:
        import executor
        eth_bal   = executor.get_eth_balance()
        eth_price = executor.get_eth_usd_price()
        usdc_addr = _cfg()['tokens']['USDC']['address']
        usdc_bal  = executor.get_token_balance(usdc_addr, decimals=6)
        wallet_usd    = round(eth_bal * eth_price + usdc_bal, 2)
        positions_usd = round(sum(p['usd_est'] for p in build_state()['active_positions']), 2)
        data = {
            'eth':            round(eth_bal, 4),
            'eth_usd':        round(eth_price, 0),
            'usdc':           round(usdc_bal, 2),
            'wallet_usd':     wallet_usd,
            'positions_usd':  positions_usd,
            'total_usd':      round(wallet_usd + positions_usd, 2),
            'from_cache':     False,
        }
    except Exception as e:
        data = {'error': str(e), 'eth': 0, 'usdc': 0, 'wallet_usd': 0, 'positions_usd': 0, 'total_usd': 0, 'from_cache': False}

    with _balance_lock:
        _balance_cache['ts']   = time.time()
        _balance_cache['data'] = data
    return data


def _validate_settings(data: dict):
    """Validate settings payload. Returns error string or None if valid."""
    tiers = data.get('usd_tiers')
    if not isinstance(tiers, list) or len(tiers) != 3:
        return 'usd_tiers must be a list of 3 tiers'
    total_weight = 0.0
    for i, t in enumerate(tiers):
        mn = t.get('min', 0)
        mx = t.get('max', 0)
        w  = t.get('weight', 0)
        if not isinstance(mn, (int, float)) or not isinstance(mx, (int, float)) or not (0 < mn < mx):
            return f'usd_tiers[{i}]: min must be > 0 and < max'
        if not isinstance(w, (int, float)) or not (0 < w <= 1):
            return f'usd_tiers[{i}]: weight must be > 0 and <= 1'
        total_weight += w
    if abs(total_weight - 1.0) > 0.01:
        return f'usd_tiers weights must sum to 1.0 (got {total_weight:.3f})'
    for i in range(len(tiers) - 1):
        if float(tiers[i]['max']) > float(tiers[i + 1]['min']):
            return f'usd_tiers[{i}].max must be <= tiers[{i+1}].min (no gaps allowed)'
    eth_min = data.get('eth_min')
    if not isinstance(eth_min, (int, float)) or not (0 < eth_min < 1.0):
        return 'eth_min must be between 0 and 1.0 ETH'
    usdc = data.get('usdc_retain_usd')
    if not isinstance(usdc, (int, float)) or usdc < 0:
        return 'usdc_retain_usd must be >= 0'
    weth = data.get('weth_retain_eth')
    if not isinstance(weth, (int, float)) or weth < 0:
        return 'weth_retain_eth must be >= 0'
    exp = data.get('expiry_days', {})
    for cat in ('lend', 'lp', 'borrow', 'vote'):
        r = exp.get(cat)
        if r is not None:
            if not isinstance(r, list) or len(r) != 2:
                return f'expiry_days.{cat} must be [min, max]'
            if not (1 <= int(r[0]) <= int(r[1]) <= 90):
                return f'expiry_days.{cat}: must satisfy 1 <= min <= max <= 90'
    mc = data.get('max_concurrent', {})
    for cat in ('lp', 'lend', 'borrow'):
        v = mc.get(cat)
        if v is not None:
            if not isinstance(v, int) or not (1 <= v <= 50):
                return f'max_concurrent.{cat} must be integer 1-50'
    return None


class Handler(SimpleHTTPRequestHandler):
    def _json(self, data, code=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == '/api/state/all':
                self._json(build_all_state())
            elif path == '/api/plan/all':
                self._json(build_all_plan())
            elif path == '/api/state':
                self._json(build_state())
            elif path == '/api/health':
                self._json(build_health())
            elif path == '/api/health/all':
                self._json(build_all_health())
            elif path == '/api/health/refresh':
                self._json(build_health(force=True))
            elif path == '/api/stats':
                self._json(build_stats())
            elif path == '/api/balance':
                self._json(build_balance())
            elif path == '/api/briefing':
                self._json(build_briefing())
            elif path == '/api/plan':
                self._json({'plan': build_briefing().get('plan', []), 'date': date.today().isoformat()})
            elif path == '/api/rule_log':
                self._json(build_rule_log())
            elif path == '/api/action_log':
                self._json(build_action_log())
            elif path == '/api/history':
                self._json(build_history())
            elif path == '/api/dust_positions':
                from clear_dust import get_dust_positions
                dust = get_dust_positions(threshold=1.0)
                self._json({'positions': dust, 'count': len(dust)})
            elif path == '/api/settings':
                import settings as _settings
                self._json(_settings.load())
            elif path == '/api/wallets':
                wallets = _wallet_mgr.load_wallets()
                active_id = os.environ.get('WALLET_ADDRESS', '').lower()
                result = []
                for w in wallets:
                    pw = _wallet_mgr.public_wallet(w)
                    pw['is_active'] = (w['address'].lower() == active_id)
                    result.append(pw)
                self._json({'wallets': result})
            elif path == '/api/wallets/setup_status':
                self._json({
                    'in_progress': _setup_status['in_progress'],
                    'wallet_id':   _setup_status['wallet_id'],
                    'step':        _setup_status['step'],
                    'error':       _setup_status['error'],
                })
            elif path == '/api/agent_log':
                log_path = os.path.join(os.path.dirname(__file__), 'logs', 'agent.log')
                try:
                    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                        lines = f.readlines()
                    tail = [l.rstrip('\n') for l in lines[-80:]]
                    self._json({'lines': tail, 'total': len(lines)})
                except FileNotFoundError:
                    self._json({'lines': ['[agent.log not found — agent not started yet]'], 'total': 0})
            else:
                super().do_GET()
        except Exception as e:
            self._json({'error': str(e)}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/reroll':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            idx = body.get('idx')
            if not isinstance(idx, int):
                self._json({'error': 'idx required (int)'}, 400)
                return
            self._json(do_reroll(idx))
            return

        if path == '/api/reconcile':
            # Synchronous — client waits (~2-5 min for all wallets)
            self._json(do_reconcile_all())
            return

        if path == '/api/sweep':
            import subprocess, sys
            subprocess.Popen(
                [sys.executable, 'sweep_tokens.py'],
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
            self._json({'ok': True, 'message': 'Sweep started — check CMD for progress'})
            return

        if path == '/api/withdraw_position':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            pos_id = body.get('position_id')
            if not isinstance(pos_id, int):
                self._json({'error': 'position_id required (int)'}, 400)
                return
            import subprocess, sys
            subprocess.Popen(
                [sys.executable, 'withdraw_all.py', '--id', str(pos_id)],
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
            # Remove manual-withdraw flag for this pos_id if it exists
            try:
                wid = os.environ.get('WALLET_ID', 'default')
                _mf = os.path.join(_CACHE_DIR, f'manual_withdraw_{wid}.json')
                if os.path.exists(_mf):
                    import json as _jmf
                    items = _jmf.load(open(_mf))
                    items = [i for i in items if i.get('pos_id') != pos_id]
                    with open(_mf, 'w') as _f: _jmf.dump(items, _f)
            except Exception:
                pass
            self._json({'ok': True, 'message': f'Withdrawing position #{pos_id} — check CMD for progress'})
            return

        # ── ALL-wallet plan endpoints ──────────────────────────────────────
        if path == '/api/plan/all/add':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            if str(body.get('pin', '')) != DASHBOARD_PIN:
                self._json({'error': 'Invalid PIN'}, 403)
                return
            self._json(do_plan_all_add())
            return

        if path == '/api/plan/all/reroll':
            self._json(do_plan_all_reroll())
            return

        if path == '/api/reroll/wallet':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            wallet_id = body.get('wallet_id')
            idx       = body.get('idx')
            if not wallet_id or not isinstance(idx, int):
                self._json({'error': 'wallet_id and idx (int) required'}, 400)
                return
            self._json(_with_wallet_context(wallet_id, lambda: do_reroll(idx)))
            return

        if path == '/api/cancel_plan/wallet':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            wallet_id = body.get('wallet_id')
            idx       = body.get('idx')
            if not wallet_id or not isinstance(idx, int):
                self._json({'error': 'wallet_id and idx (int) required'}, 400)
                return
            def _cancel():
                from daily_briefing import load_plan, get_plan_file
                import json as _json
                plan = load_plan()
                if not plan:
                    return {'error': 'No plan for today'}
                target = next((a for a in plan if a['idx'] == idx), None)
                if target is None:
                    return {'error': f'Action #{idx} not found'}
                if target.get('done', False):
                    return {'error': f'Action #{idx} already done'}
                plan = [a for a in plan if a['idx'] != idx]
                with open(get_plan_file(), 'w') as f:
                    _json.dump({'date': date.today().isoformat(), 'actions': plan}, f, indent=2)
                return {'ok': True, 'removed_idx': idx}
            self._json(_with_wallet_context(wallet_id, _cancel))
            return

        if path == '/api/reroll_all':
            try:
                from daily_briefing import load_plan
                plan = load_plan()
                if not plan:
                    self._json({'error': 'No plan for today'})
                    return
                pending = [a for a in plan if not a.get('done', False)]
                if not pending:
                    self._json({'error': 'All actions already done — nothing to reroll'})
                    return
                results = []
                for action in pending:
                    res = do_reroll(action['idx'])
                    results.append({'idx': action['idx'], 'ok': res.get('ok', False),
                                    'new_platform': res.get('new_platform'),
                                    'error': res.get('error')})
                ok_count  = sum(1 for r in results if r['ok'])
                self._json({'ok': True, 'results': results,
                            'message': f'Rerolled {ok_count}/{len(results)} actions'})
            except Exception as e:
                self._json({'error': str(e)}, 500)
            return

        if path == '/api/add_action':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            if str(body.get('pin', '')) != DASHBOARD_PIN:
                self._json({'error': 'Invalid PIN'}, 403)
                return
            import subprocess, sys
            subprocess.Popen(
                [sys.executable, 'run_now.py'],
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
            self._json({'ok': True, 'message': 'Adding action — check CMD for progress'})
            return

        if path == '/api/add_to_plan':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            if str(body.get('pin', '')) != DASHBOARD_PIN:
                self._json({'error': 'Invalid PIN'}, 403)
                return
            self._json(do_add_to_plan())
            return

        if path == '/api/clear_dust':
            import subprocess, sys
            subprocess.Popen(
                [sys.executable, 'clear_dust.py', '--live'],
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
            self._json({'ok': True, 'message': 'Dust clearing started — check CMD for progress'})
            return

        if path == '/api/settings':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                self._json({'error': 'Invalid JSON'}, 400)
                return
            err = _validate_settings(body)
            if err:
                self._json({'error': err}, 400)
                return
            import settings as _settings
            _settings.save(body)
            self._json({'ok': True, 'settings': body})
            return

        if path == '/api/cancel_plan':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            idx = body.get('idx')
            if not isinstance(idx, int):
                self._json({'error': 'idx required (int)'}, 400)
                return
            try:
                from daily_briefing import load_plan, get_plan_file
                plan = load_plan()
                if not plan:
                    self._json({'error': 'No plan for today'})
                    return
                target = next((a for a in plan if a['idx'] == idx), None)
                if target is None:
                    self._json({'error': f'Action #{idx} not found'})
                    return
                if target.get('done', False):
                    self._json({'error': f'Action #{idx} already done'})
                    return
                plan = [a for a in plan if a['idx'] != idx]
                import json as _json
                with open(get_plan_file(), 'w') as f:
                    _json.dump({'date': date.today().isoformat(), 'actions': plan}, f, indent=2)
                self._json({'ok': True, 'removed_idx': idx})
            except Exception as e:
                self._json({'error': str(e)}, 500)
            return

        if path == '/api/settings/reset':
            import settings as _settings
            d = _settings.reset()
            self._json({'ok': True, 'settings': d})
            return

        if path == '/api/emergency_close':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}

            if str(body.get('pin', '')) != DASHBOARD_ADMIN_PIN:
                self._json({'error': 'Invalid PIN'}, 403)
                return

            import subprocess, sys
            subprocess.Popen(
                [sys.executable, 'withdraw_all.py', '--emergency'],
                cwd=os.path.dirname(os.path.abspath(__file__))
            )
            self._json({'ok': True, 'message': 'Emergency close initiated — check CMD for progress'})
            return

        if path == '/api/wallets/switch':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            wallet_id = body.get('id')
            if not wallet_id:
                self._json({'error': 'id required'}, 400)
                return
            ok, err = _wallet_mgr.switch_context(wallet_id)
            if not ok:
                self._json({'error': err}, 400)
                return
            _clear_all_caches()
            try:
                state.init_db()
            except Exception as e:
                self._json({'error': f'DB init failed: {e}'}, 500)
                return
            self._json({'ok': True, 'wallet_id': wallet_id,
                        'address': os.environ.get('WALLET_ADDRESS', '')})
            return

        if path == '/api/wallets/toggle':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            wallet_id = body.get('id')
            if not wallet_id:
                self._json({'error': 'id required'}, 400)
                return
            ok, new_active, err = _wallet_mgr.toggle_active(wallet_id)
            if not ok:
                self._json({'error': err}, 400)
                return
            self._json({'ok': True, 'id': wallet_id, 'active': new_active})
            return

        if path == '/api/wallets/delete':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                self._json({'error': 'Invalid JSON'}, 400)
                return
            wallet_id = body.get('id', '').strip()
            pin       = body.get('pin', '').strip()
            if not wallet_id or not pin:
                self._json({'error': 'id and pin required'}, 400)
                return
            ok, err = _wallet_mgr.remove_wallet(wallet_id, pin)
            if not ok:
                self._json({'error': err}, 400)
                return
            # If deleted wallet was active context, switch to first remaining
            remaining = _wallet_mgr.load_wallets()
            if os.environ.get('WALLET_ID') == wallet_id and remaining:
                _wallet_mgr.switch_context(remaining[0]['id'])
                _clear_all_caches()
            sep = '=' * 55
            print(f'\n{sep}')
            print(f'  WALLET REMOVED: {wallet_id}')
            print(f'  (state DB kept on disk)')
            print(f'{sep}')
            self._json({'ok': True, 'wallet_id': wallet_id,
                        'remaining': len(remaining)})
            return

        if path == '/api/wallets/add':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                self._json({'error': 'Invalid JSON'}, 400)
                return
            name        = body.get('name', '').strip()
            address     = body.get('address', '').strip()
            private_key = body.get('private_key', '').strip()
            delete_pin  = body.get('delete_pin', '').strip()
            if not name or not address:
                self._json({'error': 'name and address required'}, 400)
                return
            with _setup_lock_obj:
                if _setup_status['in_progress']:
                    self._json({'error': f'Setup in progress for wallet "{_setup_status["wallet_id"]}" — wait until complete'}, 409)
                    return
                entry, err = _wallet_mgr.add_wallet(name, address, private_key, delete_pin)
                if err:
                    self._json({'error': err}, 400)
                    return
                _setup_status['in_progress'] = True
                _setup_status['wallet_id']   = entry['id']
                _setup_status['step']        = 'starting'
                _setup_status['error']       = None
            sep = '=' * 55
            print(f'\n{sep}')
            print(f'  NEW WALLET: {entry["name"]}  ({entry["id"]})')
            print(f'  Address  : {entry["address"]}')
            print(f'  DB       : {entry["state_db"]}')
            print(f'  On-chain recovery starting...')
            print(f'{sep}')
            t = threading.Thread(target=_run_wallet_setup, args=(entry['id'],), daemon=True)
            t.start()
            self._json({'ok': True, 'wallet_id': entry['id'],
                        'message': 'Wallet added — on-chain recovery running in background'})
            return

        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    # Init wallet context from wallets.json (falls back to .env if file missing)
    _wid = _wallet_mgr.get_last_active()
    if _wid:
        ok, err = _wallet_mgr.switch_context(_wid)
        if not ok:
            print(f'[wallet] Warning: {err} — using .env defaults')
    state.init_db()
    print(f'Dashboard   : http://localhost:{PORT}/dashboard.html')
    print(f'API /state  : http://localhost:{PORT}/api/state')
    print(f'API /health : http://localhost:{PORT}/api/health  (cache {HEALTH_CACHE_TTL}s)')
    print(f'API /stats  : http://localhost:{PORT}/api/stats')
    print(f'API /balance: http://localhost:{PORT}/api/balance  (cache {BALANCE_CACHE_TTL}s)')
    HTTPServer(('', PORT), Handler).serve_forever()
