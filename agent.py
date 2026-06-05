import os, json, random, logging, threading, importlib, sys
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
import wallet_manager as _wm

import state
import executor
import swap
import aero_vote as _aero_vote
import megapot as _megapot
import deploy_contract as _deploy
import uni_lp as _uni_lp
import pancake_lp as _pancake_lp
import compound_borrow as _compound_borrow
import moonwell_borrow as _mw_borrow
import fluid_borrow as _fl_borrow
import aave_supply as _aave_supply
import aave_borrow as _aave_borrow
import health_monitor as _health_monitor
import time
import rule_engine as _rule_engine
import portfolio_tracker as _portfolio_tracker
import weekly_report as _weekly_report
from swap import PriceGuardError, ConfigError, SwapExecutionError

load_dotenv()

import settings as _settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler('logs/agent.log'),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as f:
    CFG = json.load(f)

USDC_ADDR        = CFG['tokens']['USDC']['address']
USDS_ADDR        = CFG['tokens']['USDS']['address']
SUSDS_ADDR       = CFG['tokens']['sUSDS']['address']
ACTIVE_PLATFORMS = (CFG['phase1'] + CFG.get('phase2', []) + CFG.get('phase3', []) +
                    CFG.get('phase4', []) + CFG.get('phase5', []) + CFG.get('phase_aero_lp', []) +
                    CFG.get('phase_uni_lp', []) + CFG.get('phase_pancake_lp', []) +
                    CFG.get('phase_borrow', []) + CFG.get('phase_mw_borrow', []) +
                    CFG.get('phase_fluid_borrow', []) +
                    CFG.get('phase_aave_supply', []) +
                    CFG.get('phase_aave_borrow', []))

AERO_LP_BUDGET_USD    = 5.0  # default — overridden at runtime by plan usd_est
UNI_LP_BUDGET_USD     = 5.0
PANCAKE_LP_BUDGET_USD = 5.0
BEEFY_LP_BUDGET_USD   = 5.0
# Stablecoins with no Uniswap v3 pool — acquire via ETH->USDC->token via Aerodrome sAMM
AERO_STABLE_ONLY_TOKENS = {'DOLA', 'USDz'}

def _pname(platform: str, p: dict) -> str:
    """Human-readable borrow pair name for logs. Falls back to key if display_name absent."""
    return p.get('display_name', platform)


def _token_addr(platform_key: str) -> str:
    p = CFG['platforms'][platform_key]
    return p.get('token_address', USDC_ADDR)

_AMOUNT_OVERRIDE:  dict = {}  # platform_key -> amount_wei (set from plan usd_est)
_EXPIRY_OVERRIDE:  dict = {}  # platform_key -> expiry_days int (set from plan expiry_days)


def _expiry_for(platform_key: str, p_cfg: dict) -> int:
    """Expiry days: settings (type-based) overrides contracts.json per-platform range."""
    ptype = p_cfg.get('type', '')
    try:
        return _settings.expiry_for_type(ptype)
    except Exception:
        return random.randint(*p_cfg.get('expiry_days', [3, 5]))

def _amount(platform_key: str) -> int:
    if platform_key in _AMOUNT_OVERRIDE:
        return _AMOUNT_OVERRIDE[platform_key]
    p_cfg2   = CFG['platforms'].get(platform_key, {})
    token    = p_cfg2.get('token') or p_cfg2.get('borrow_token', '')
    tok      = CFG['tokens'].get(token, {})
    decimals = tok.get('decimals', 18)
    pos_amt  = tok.get('position_amount', 0.01)
    env_val  = os.getenv(f'POSITION_{token.upper()}_AMOUNT')
    if env_val:
        pos_amt = float(env_val)
    return int(round(pos_amt * 10**decimals))

def _amount_from_usd(platform_key: str, usd: float) -> int:
    """Convert USD amount to token wei using live price."""
    p = CFG['platforms'].get(platform_key, {})
    token = p.get('token') or p.get('borrow_token', '')
    tok = CFG['tokens'].get(token, {})
    decimals = tok.get('decimals', 18)
    STABLES = {'USDC', 'USDS', 'sUSDS', 'EURC', 'USDT', 'DOLA', 'USDz'}
    if token in STABLES:
        price = 1.0
    else:
        try:
            price = executor.get_token_usd_price(token)
        except Exception:
            price = 1.0
    token_amt = usd / price if price > 0 else tok.get('position_amount', 5.0)
    return int(round(token_amt * 10**decimals))

def _lp_token_amounts(p: dict) -> tuple:
    """Return (token0_addr, token1_addr, stable, amt0_wei, amt1_wei) for beefy_lp."""
    t0_addr = executor.Web3.to_checksum_address(p['token0_address'])
    t1_addr = executor.Web3.to_checksum_address(p['token1_address'])
    stable  = p.get('stable', False)
    t0_tok  = CFG['tokens'].get(p['token0'], {})
    t1_tok  = CFG['tokens'].get(p['token1'], {})
    amt0    = int(round(t0_tok.get('position_amount', 0.01) * 10**t0_tok.get('decimals', 18)))
    amt1    = int(round(t1_tok.get('position_amount', 0.01) * 10**t1_tok.get('decimals', 18)))
    return t0_addr, t1_addr, stable, amt0, amt1


def _supply(platform_key: str) -> str:
    p    = CFG['platforms'][platform_key]
    addr = p['address']
    tok  = _token_addr(platform_key)
    amt  = _amount(platform_key)
    t    = p['type']
    if t == 'comet':
        return executor.compound_supply(addr, tok, amt)
    if t == 'erc4626':
        return executor.erc4626_deposit(addr, tok, amt)
    if t == 'ctoken':
        return executor.ctoken_supply(addr, tok, amt)
    if t == 'psm_hold':
        susds = executor.w3.eth.contract(
            address=executor.Web3.to_checksum_address(SUSDS_ADDR),
            abi=executor.ERC20_ABI,
        )
        bal = susds.functions.balanceOf(executor.WALLET).call()
        return f'psm_hold_{bal}'
    if t == 'beefy_single':
        return executor.beefy_deposit(addr, tok, amt)
    if t == 'beefy_lp':
        import time as _time
        t0 = executor.Web3.to_checksum_address(p['token0_address'])
        t1 = executor.Web3.to_checksum_address(p['token1_address'])
        stable  = p.get('stable', False)
        lp_addr = executor.Web3.to_checksum_address(p['lp_address'])
        lp_c = executor.w3.eth.contract(address=lp_addr, abi=executor.ERC20_ABI)
        t0_c = executor.w3.eth.contract(address=t0, abi=executor.ERC20_ABI)
        t1_c = executor.w3.eth.contract(address=t1, abi=executor.ERC20_ABI)
        _time.sleep(4)  # let RPC settle after token acquisition
        amt0 = t0_c.functions.balanceOf(executor.WALLET).call()
        amt1 = t1_c.functions.balanceOf(executor.WALLET).call()
        if amt0 == 0 or amt1 == 0:
            raise RuntimeError(f'beefy_lp: zero token balance t0={amt0} t1={amt1}')
        lp_before = lp_c.functions.balanceOf(executor.WALLET).call()
        executor.aerodrome_add_liquidity(t0, t1, stable, amt0, amt1)
        _time.sleep(4)
        lp_after = lp_c.functions.balanceOf(executor.WALLET).call()
        lp_recv  = max(lp_after - lp_before, 0)
        if lp_recv == 0:
            raise RuntimeError(f'beefy_lp: no LP received after addLiquidity')
        return executor.beefy_deposit(addr, lp_addr, lp_recv)
    if t == 'aero_lp':
        import time
        t0 = executor.Web3.to_checksum_address(p['token0_address'])
        t1 = executor.Web3.to_checksum_address(p['token1_address'])
        stable     = p.get('stable', False)
        pool_addr  = executor.Web3.to_checksum_address(p['pool_address'])
        gauge_addr = executor.Web3.to_checksum_address(p['gauge_address'])
        lp_c = executor.w3.eth.contract(address=pool_addr,  abi=executor.ERC20_ABI)
        t0_c = executor.w3.eth.contract(address=t0, abi=executor.ERC20_ABI)
        t1_c = executor.w3.eth.contract(address=t1, abi=executor.ERC20_ABI)
        # Use actual wallet balance — sleep lets RPC settle after wrap TX
        time.sleep(4)
        amt0 = t0_c.functions.balanceOf(executor.WALLET).call()
        amt1 = t1_c.functions.balanceOf(executor.WALLET).call()
        if amt0 == 0 or amt1 == 0:
            raise RuntimeError(f'aero_lp: zero token balance t0={amt0} t1={amt1}')
        lp_before = lp_c.functions.balanceOf(executor.WALLET).call()
        executor.aerodrome_add_liquidity(t0, t1, stable, amt0, amt1)
        time.sleep(4)
        lp_after = lp_c.functions.balanceOf(executor.WALLET).call()
        lp_recv  = max(lp_after - lp_before, 0)
        if lp_recv == 0:
            raise RuntimeError(f'aero_lp: no LP received after addLiquidity')
        return executor.aerodrome_gauge_stake(pool_addr, gauge_addr, lp_recv)
    if t == 'aave_supply':
        return _aave_supply.supply(p['token_address'], amt)
    raise ValueError(f'Unknown type: {t}')


def _withdraw(platform_key: str, amount_wei: int) -> str:
    p    = CFG['platforms'][platform_key]
    addr = p['address']
    tok  = _token_addr(platform_key)
    t    = p['type']
    if t == 'comet':
        return executor.compound_withdraw(addr, tok, amount_wei)
    if t == 'erc4626':
        return executor.erc4626_withdraw_all(addr)
    if t == 'ctoken':
        return executor.ctoken_withdraw_all(addr)
    if t == 'psm_hold':
        usdc_received = executor.psm_swap_susds_to_usdc(int(float(amount_wei)))
        return f'psm_sell_{usdc_received}'
    if t == 'beefy_single':
        return executor.beefy_withdraw_all(addr)
    if t == 'beefy_lp':
        # Step 1: Beefy withdraw → LP token in wallet
        executor.beefy_withdraw_all(addr)
        import time; time.sleep(4)
        lp_addr = executor.Web3.to_checksum_address(p['lp_address'])
        lp_c    = executor.w3.eth.contract(address=lp_addr, abi=executor.ERC20_ABI)
        lp_bal  = lp_c.functions.balanceOf(executor.WALLET).call()
        if lp_bal == 0:
            return 'beefy_lp_withdraw_no_lp'
        # Step 2: Remove liquidity → token0 + token1
        t0, t1, stable, _, _ = _lp_token_amounts(p)
        executor.aerodrome_remove_liquidity(t0, t1, stable, lp_bal)
        return f'beefy_lp_withdraw_{lp_bal}'
    if t == 'uni_lp':
        return _uni_lp.close_uni_lp(amount_wei)
    if t == 'pancake_lp':
        return _pancake_lp.close_pancake_lp(amount_wei)
    if t == 'aero_lp':
        import time
        gauge_addr = executor.Web3.to_checksum_address(p['gauge_address'])
        pool_addr  = executor.Web3.to_checksum_address(p['pool_address'])
        gauge_c    = executor.w3.eth.contract(address=gauge_addr, abi=executor.GAUGE_ABI)
        staked     = gauge_c.functions.balanceOf(executor.WALLET).call()
        # Step 1: claim rewards first (best-effort)
        try:
            executor.aerodrome_gauge_claim(gauge_addr)
        except Exception as e:
            log.warning(f'aero_lp gauge_claim skipped: {e}')
        # Step 2: unstake all LP from gauge
        if staked == 0:
            return 'aero_lp_no_staked'
        executor.aerodrome_gauge_unstake(gauge_addr, staked)
        time.sleep(4)
        # Step 3: remove liquidity → token0 + token1
        t0, t1, stable, _, _ = _lp_token_amounts(p)
        lp_c  = executor.w3.eth.contract(address=pool_addr, abi=executor.ERC20_ABI)
        lp_bal = lp_c.functions.balanceOf(executor.WALLET).call()
        if lp_bal == 0:
            return 'aero_lp_withdraw_no_lp'
        executor.aerodrome_remove_liquidity(t0, t1, stable, lp_bal)
        return f'aero_lp_withdraw_{lp_bal}'
    if t == 'aave_supply':
        return _aave_supply.withdraw_all(p['token_address'])
    raise ValueError(f'Unknown type: {t}')

MAX_DAILY_FAILURES = 3

MEGAPOT_INTERVAL_DAYS      = 7   # once per week
DEPLOY_CONTRACT_INTERVAL_DAYS = 14  # once per 2 weeks


def _days_since(platform: str) -> int:
    """Days since last state.db entry for platform. 999 if never run."""
    last = state.get_last_entry_date(platform)
    if not last:
        return 999
    return (date.today() - date.fromisoformat(last)).days


def _check_borrow_health(failed: list):
    """Daily health check for all active borrow positions via health_monitor."""
    results = _health_monitor.check_all()
    for r in results:
        platform = r['platform']
        p        = CFG['platforms'].get(platform, {})
        ptype    = r['ptype']
        health   = r['health']
        log.info(f'borrow health [{_pname(platform, p)}]: {health:.2f}x  ({r["status"]})')

        if r['status'] == 'ERROR':
            log.warning(f'health check error for {platform} — skipping close')
            continue

        threshold = _health_monitor.HEALTH_OK
        if health < threshold:
            log.warning(
                f'EARLY CLOSE: {_pname(platform, p)} health={health:.2f}x < {threshold}x — closing now'
            )
            try:
                pos_id   = r['pos_id']
                encoded  = r['encoded']
                if ptype == 'compound_borrow':
                    txh = _compound_borrow.close_borrow(encoded, p)
                    state.close_position(pos_id)
                    state.record_cooldown(platform)
                    log.info(f'Early closed {_pname(platform, p)}')
                elif ptype == 'mw_borrow':
                    _mw_borrow.close_borrow(encoded, p, pos_id)
                    state.record_cooldown(platform)
                    log.info(f'Early closed {_pname(platform, p)}')
                elif ptype == 'fluid_borrow':
                    txh = _fl_borrow.close_borrow(encoded, p)
                    state.close_position(pos_id)
                    state.record_cooldown(platform)
                    log.info(f'Early closed {_pname(platform, p)}')
                elif ptype == 'aave_borrow':
                    txh = _aave_borrow.close_borrow(encoded, p)
                    state.close_position(pos_id)
                    state.record_cooldown(platform)
                    log.info(f'Early closed {_pname(platform, p)}')
                else:
                    log.error(f'Unknown borrow ptype [{ptype}] for {platform} — cannot close')
            except Exception as e:
                log.error(f'Early close failed [{_pname(platform, p)}]: {e}')
                failed.append(f'early_close_{platform}')


def _run_periodic_actions(failed: list):
    """
    Runs before supply/withdraw cycle each day.

    borrow_health: daily health check — close early if health < 1.5x
    aero_vote    : weekly lock cycle — enter new 7d lock if none active, exit when expired
    megapot      : buy 1 ticket per week
    deploy       : deploy 1 ERC20 per 2 weeks
    """
    # ── aero_vote ─────────────────────────────────────────────────────────────
    import sqlite3 as _sqlite3
    active_votes = state.get_active('aero_vote')

    if active_votes:
        for pos in active_votes:
            pos_id, platform, token, amount_wei, entry, expiry, tx_hash, *_rest = pos
            try:
                token_id = int(str(amount_wei).split('|')[0])
            except Exception:
                log.warning(f'aero_vote: cannot parse tokenId from amount_wei={amount_wei!r}')
                continue

            if expiry <= date.today().isoformat():
                log.info(f'aero_vote: lock expired ({expiry}) — exiting tokenId={token_id}')
                try:
                    txh = _aero_vote.aero_vote_exit(token_id)
                    state.close_position(pos_id)
                    log.info(f'aero_vote exit done -> {txh}')
                except RuntimeError as e:
                    if 'LOCKED_SKIP' in str(e):
                        log.info(f'aero_vote: {e}')
                    else:
                        log.error(f'aero_vote exit failed: {e}')
                        failed.append('aero_vote_exit')
            else:
                days_left = (date.fromisoformat(expiry) - date.today()).days
                log.info(f'aero_vote: tokenId={token_id} still locked ({days_left}d left) — skip')

    else:
        # No active position — start a new 7-day lock cycle
        log.info('aero_vote: no active position — entering new 7d lock cycle')
        executor._local_nonce = None
        try:
            result = _aero_vote.aero_vote_enter(lock_days=7)
            if not executor.DRY_RUN:
                token_id     = result['token_id']
                aero_wei     = result['aero_wei']
                lock_end_str = date.fromtimestamp(result['lock_end']).isoformat()
                amount_str   = f"{token_id}|{aero_wei}"
                with _sqlite3.connect(state.DB_PATH) as _c:
                    _c.execute(
                        'INSERT INTO positions (platform,token,amount_wei,entry_date,expiry_date,tx_hash,status) VALUES (?,?,?,?,?,?,?)',
                        ('aero_vote', 'AERO', amount_str, date.today().isoformat(), lock_end_str, result['tx_lock'], 'active'),
                    )
                state.log_daily_stat('vote')
                log.info(f'aero_vote enter done  tokenId={token_id}  lock_end={lock_end_str}')
            else:
                log.info(f'[DRY RUN] aero_vote_enter done (no DB write)')
        except Exception as e:
            log.error(f'aero_vote enter failed: {e}')
            failed.append('aero_vote_enter')

    # ── megapot (once per week) ───────────────────────────────────────────────
    days_mp = _days_since('megapot')
    if days_mp >= MEGAPOT_INTERVAL_DAYS:
        log.info(f'megapot: last {days_mp}d ago — buying ticket')
        _megapot._local_nonce = None  # reset nonce before fresh run
        try:
            txh, mode = _megapot.buy_ticket()
            if not _megapot.DRY_RUN:
                _megapot._record(txh, mode)
                state.log_daily_stat('game')
            log.info(f'megapot done  mode={mode}  tx={txh}')
        except Exception as e:
            log.error(f'megapot failed: {e}')
            failed.append('megapot')
    else:
        log.info(f'megapot: last {days_mp}d ago — skip (< {MEGAPOT_INTERVAL_DAYS}d)')

    # ── deploy_contract (once per 2 weeks) ───────────────────────────────────
    days_dc = _days_since('deploy_contract')
    if days_dc >= DEPLOY_CONTRACT_INTERVAL_DAYS:
        log.info(f'deploy_contract: last {days_dc}d ago — deploying')
        _deploy._local_nonce = None  # reset nonce before fresh run
        try:
            txh, addr, name, symbol = _deploy.deploy_one()
            if not _deploy.DRY_RUN:
                _deploy._record(txh, addr, symbol)
                state.log_daily_stat('deploy')
            log.info(f'deploy_contract done  name={name}  addr={addr}  tx={txh}')
        except Exception as e:
            log.error(f'deploy_contract failed: {e}')
            failed.append('deploy_contract')
    else:
        log.info(f'deploy_contract: last {days_dc}d ago — skip (< {DEPLOY_CONTRACT_INTERVAL_DAYS}d)')


def _prepare_token_safe(p: dict, tok_addr: str, amt: int, failed: list) -> bool:
    """ETH -> token(s) before supply. Handles all platform types."""
    token = p.get('token', '')   # uni_lp/pancake_lp have no plain 'token' key
    ptype = p['type']

    # compound_borrow: open_borrow_usdc handles ETH->WETH wrap internally
    if ptype == 'compound_borrow':
        return True

    # uni_lp / pancake_lp: full-range v3 — 50:50 USD split (proven optimal for any full-range position)
    if ptype in ('uni_lp', 'pancake_lp'):
        t0_sym  = p['token0']
        t1_sym  = p['token1']
        t0_addr = executor.Web3.to_checksum_address(p['token0_address'])
        t1_addr = executor.Web3.to_checksum_address(p['token1_address'])
        p0 = executor.get_token_usd_price(t0_sym)
        p1 = executor.get_token_usd_price(t1_sym)
        t0_dec = CFG['tokens'].get(t0_sym, {}).get('decimals', 18)
        t1_dec = CFG['tokens'].get(t1_sym, {}).get('decimals', 18)
        budget = PANCAKE_LP_BUDGET_USD if ptype == 'pancake_lp' else UNI_LP_BUDGET_USD
        half = budget / 2
        amt0 = int(half / p0 * 10**t0_dec)
        amt1 = int(half / p1 * 10**t1_dec)
        # non-WETH first to avoid WETH being silently unwrapped by subsequent swap._unwrap_weth
        pairs = [(t0_sym, t0_addr, amt0), (t1_sym, t1_addr, amt1)]
        pairs.sort(key=lambda x: 1 if x[0] == 'WETH' else 0)
        for sym, addr, amount in pairs:
            if sym == 'WETH':
                try:
                    swap.wrap_eth(amount)
                except Exception as e:
                    log.error(f'Wrap WETH failed for {p["name"]}: {e}')
                    failed.append(f'wrap_weth_{p["name"]}')
                    return False
            else:
                try:
                    swap.attempt_swap(swap.swap_eth_to_token, addr, amount)
                except (PriceGuardError, ConfigError) as e:
                    log.warning(f'Swap->{sym} skipped for {p["name"]}: {e}')
                    failed.append(f'swap_to_{sym}_{p["name"]}')
                    return False
                except SwapExecutionError as e:
                    log.error(f'Swap->{sym} failed for {p["name"]}: {e}')
                    failed.append(f'swap_to_{sym}_{p["name"]}')
                    return False
        return True

    # beefy_lp / aero_lp: swap ETH -> token0 + token1 separately
    if ptype in ('beefy_lp', 'aero_lp'):
        if ptype == 'aero_lp':
            # Dynamic: split budget by current pool ratio — no residuals
            t0_addr, t1_addr, _, amt0, amt1 = executor.get_aero_lp_deposit_amounts(
                p, CFG['tokens'], AERO_LP_BUDGET_USD
            )
        else:
            # beefy_lp: 50:50 USD split from budget (same pattern as uni/pancake_lp)
            t0_sym  = p['token0']
            t1_sym  = p['token1']
            t0_addr = executor.Web3.to_checksum_address(p['token0_address'])
            t1_addr = executor.Web3.to_checksum_address(p['token1_address'])
            t0_dec  = CFG['tokens'].get(t0_sym, {}).get('decimals', 18)
            t1_dec  = CFG['tokens'].get(t1_sym, {}).get('decimals', 18)
            p0 = executor.get_token_usd_price(t0_sym)
            p1 = executor.get_token_usd_price(t1_sym)
            half = BEEFY_LP_BUDGET_USD / 2
            amt0 = int(half / p0 * 10**t0_dec) if p0 > 0 else 0
            amt1 = int(half / p1 * 10**t1_dec) if p1 > 0 else 0
        t0_sym = p['token0']
        t1_sym = p['token1']

        # non-WETH first: DEX swap residual-unwrap would wipe any WETH already wrapped
        pairs = [(t0_sym, t0_addr, amt0), (t1_sym, t1_addr, amt1)]
        if ptype in ('aero_lp', 'beefy_lp'):
            pairs.sort(key=lambda x: 1 if x[0] == 'WETH' else 0)

        for sym, addr, amt in pairs:
            if amt == 0:
                continue  # pool ratio is 0% for this token — skip
            if sym == 'WETH':
                try:
                    swap.wrap_eth(amt)
                except Exception as e:
                    log.error(f'Wrap WETH failed: {e}')
                    failed.append(f'wrap_weth_{p["name"]}')
                    return False
            elif sym in AERO_STABLE_ONLY_TOKENS:
                # No Uniswap v3 pool: ETH->USDC then Aerodrome sAMM USDC->token
                usdc_amt = int(amt // 10**12)  # 18-dec -> 6-dec (1:1 stable peg)
                try:
                    swap.attempt_swap(swap.swap_eth_to_token, USDC_ADDR, usdc_amt)
                except (PriceGuardError, ConfigError, SwapExecutionError) as e:
                    log.error(f'ETH->USDC for {sym} failed: {e}')
                    failed.append(f'swap_eth_usdc_for_{sym}')
                    return False
                try:
                    executor.aerodrome_swap_stable(USDC_ADDR, addr, usdc_amt)
                except Exception as e:
                    log.error(f'Aerodrome USDC->{sym} failed: {e}')
                    failed.append(f'aero_swap_usdc_{sym}')
                    return False
            else:
                try:
                    swap.attempt_swap(swap.swap_eth_to_token, addr, amt)
                except (PriceGuardError, ConfigError) as e:
                    log.warning(f'Swap->{sym} skipped: {e}')
                    failed.append(f'swap_to_{sym}_{p["name"]}')
                    return False
                except SwapExecutionError as e:
                    log.error(f'Swap->{sym} failed: {e}')
                    failed.append(f'swap_to_{sym}_{p["name"]}')
                    return False
        return True

    if token == 'ETH':
        return True
    if token == 'WETH':
        try:
            swap.wrap_eth(amt)
            return True
        except Exception as e:
            log.error(f'Wrap failed: {e}')
            failed.append(f'wrap_{p["name"]}')
            return False
    if token == 'USDS':
        # 2-step: ETH → USDC (DEX exactOutput) → USDS (PSM 1:1)
        usdc_amount = amt // 10**12  # USDS 18dec → USDC 6dec
        try:
            swap.attempt_swap(swap.swap_eth_to_token, USDC_ADDR, usdc_amount)
        except (PriceGuardError, ConfigError) as e:
            log.warning(f'ETH→USDC swap skipped (no-retry): {e}')
            failed.append(f'swap_eth_usdc_for_{p["name"]}')
            return False
        except SwapExecutionError as e:
            log.error(f'ETH→USDC swap failed: {e}')
            failed.append(f'swap_eth_usdc_for_{p["name"]}')
            return False
        try:
            executor.psm_swap_usdc_to_usds(usdc_amount)
            return True
        except Exception as e:
            log.error(f'PSM USDC→USDS failed: {e}')
            failed.append('psm_usdc_usds')
            return False
    if token == 'sUSDS':
        # 2-step: ETH → USDC (DEX exactOutput) → sUSDS (PSM swapExactIn)
        # amt is in USDC wei (position_amount=5, decimals=6 → 5_000_000)
        usdc_amount = amt  # already in USDC 6-decimal units
        try:
            swap.attempt_swap(swap.swap_eth_to_token, USDC_ADDR, usdc_amount)
        except (PriceGuardError, ConfigError) as e:
            log.warning(f'ETH→USDC swap skipped (no-retry): {e}')
            failed.append(f'swap_eth_usdc_for_{p["name"]}')
            return False
        except SwapExecutionError as e:
            log.error(f'ETH→USDC swap failed: {e}')
            failed.append(f'swap_eth_usdc_for_{p["name"]}')
            return False
        try:
            executor.psm_swap_usdc_to_susds(usdc_amount)
            return True
        except Exception as e:
            log.error(f'PSM USDC→sUSDS failed: {e}')
            failed.append('psm_usdc_susds')
            return False
    # ERC20
    try:
        swap.attempt_swap(swap.swap_eth_to_token, tok_addr, amt)
        return True
    except (PriceGuardError, ConfigError) as e:
        log.warning(f'Swap skipped (no-retry): {e}')
        failed.append(f'swap_to_{token}')
        return False
    except SwapExecutionError as e:
        log.error(f'Swap failed after retries: {e}')
        failed.append(f'swap_to_{token}')
        return False


def _return_to_eth_safe(p: dict, tok_addr: str, amount_wei: int, failed: list) -> bool:
    """token(s) -> ETH after withdraw. Handles all platform types."""
    token = p.get('token', '')   # uni_lp/pancake_lp have no plain 'token' key
    ptype = p['type']

    # compound_borrow: close_borrow_usdc handles repay+collateral withdrawal+WETH->ETH internally
    if ptype == 'compound_borrow':
        return True

    # beefy_lp / aero_lp / uni_lp / pancake_lp: _withdraw already removed liquidity → token0 + token1 in wallet
    if ptype in ('beefy_lp', 'aero_lp', 'uni_lp', 'pancake_lp'):
        t0_addr, t1_addr, _, _, _ = _lp_token_amounts(p)
        t0_tok = p['token0']
        t1_tok = p['token1']
        ok = True
        for t_addr, t_sym in [(t0_addr, t0_tok), (t1_addr, t1_tok)]:
            c = executor.w3.eth.contract(
                address=executor.Web3.to_checksum_address(t_addr), abi=executor.ERC20_ABI
            )
            bal = c.functions.balanceOf(executor.WALLET).call()
            if bal == 0:
                continue
            if t_sym == 'WETH':
                try:
                    swap.unwrap_all_weth()
                except Exception as e:
                    log.error(f'Unwrap WETH failed: {e}')
                    failed.append(f'unwrap_weth_{p["name"]}')
                    ok = False
            else:
                try:
                    swap.attempt_swap(swap.swap_token_to_eth, t_addr, bal)
                except (PriceGuardError, ConfigError) as e:
                    log.warning(f'Swap {t_sym}→ETH skipped: {e}')
                    failed.append(f'swap_{t_sym}_eth_{p["name"]}')
                    ok = False
                except SwapExecutionError as e:
                    log.error(f'Swap {t_sym}→ETH failed: {e}')
                    failed.append(f'swap_{t_sym}_eth_{p["name"]}')
                    ok = False
        return ok

    # beefy_single: same as underlying token
    if token == 'ETH':
        return True
    if token == 'WETH':
        try:
            swap.unwrap_all_weth()
            return True
        except Exception as e:
            log.error(f'Unwrap failed: {e}')
            failed.append(f'unwrap_{p["name"]}')
            return False
    if token == 'USDS':
        # 2-step: USDS → USDC (PSM 1:1) → ETH (DEX)
        try:
            usdc_received = executor.psm_swap_usds_to_usdc(amount_wei)
        except Exception as e:
            log.error(f'PSM USDS→USDC failed: {e}')
            failed.append('psm_usds_usdc')
            return False
        try:
            swap.attempt_swap(swap.swap_token_to_eth, USDC_ADDR, usdc_received)
            return True
        except (PriceGuardError, ConfigError) as e:
            log.warning(f'USDC→ETH swap skipped (no-retry): {e}')
            failed.append('swap_usdc_eth_after_usds')
            return False
        except SwapExecutionError as e:
            log.error(f'USDC→ETH swap failed: {e}')
            failed.append('swap_usdc_eth_after_usds')
            return False
    if token == 'sUSDS':
        # _withdraw already sold sUSDS→USDC via PSM; just swap remaining USDC→ETH
        try:
            usdc_c = executor.w3.eth.contract(
                address=executor.Web3.to_checksum_address(USDC_ADDR),
                abi=executor.ERC20_ABI,
            )
            usdc_bal = usdc_c.functions.balanceOf(executor.WALLET).call()
            if usdc_bal == 0:
                return True
            swap.attempt_swap(swap.swap_token_to_eth, USDC_ADDR, usdc_bal)
            return True
        except (PriceGuardError, ConfigError) as e:
            log.warning(f'USDC→ETH swap skipped after sUSDS sell: {e}')
            failed.append('swap_usdc_eth_after_susds')
            return False
        except SwapExecutionError as e:
            log.error(f'USDC→ETH swap failed after sUSDS sell: {e}')
            failed.append('swap_usdc_eth_after_susds')
            return False
    # ERC20
    try:
        swap.attempt_swap(swap.swap_token_to_eth, tok_addr, amount_wei)
        return True
    except (PriceGuardError, ConfigError) as e:
        log.warning(f'Swap-back skipped (no-retry): {e}')
        failed.append(f'swap_back_{token}')
        return False
    except SwapExecutionError as e:
        log.error(f'Swap-back failed after retries: {e}')
        failed.append(f'swap_back_{token}')
        return False


def daily_job():
    log.info('=== daily job start ===')
    state.init_db()
    executor._local_nonce = None  # Rule 15: fresh nonce at job start

    # Rule 13: random start time 06:00-20:00 (scheduler fires at 06:00)
    delay = _rule_engine.pick_start_delay_secs()
    log.info(f'Rule 13: random delay {delay/3600:.2f}h before first action')
    time.sleep(delay)

    eth  = executor.get_eth_balance()
    usdc = executor.get_token_balance(USDC_ADDR, decimals=6)
    log.info(f'Balances — ETH: {eth:.5f}  USDC: {usdc:.2f}')

    # Rule 5: balance guard
    if not _rule_engine.balance_guard(eth):
        log.warning(f'Rule 5: ETH {eth:.5f} < {_rule_engine.get_eth_min()} — skipping today')
        return

    failed_today = []

    # 0a. Health check + early close (Rule 16/17)
    _check_borrow_health(failed_today)
    # 0b. Periodic weekly actions (Rules 21-23: megapot, deploy, aero_vote)
    _run_periodic_actions(failed_today)

    # 0c. Rule 6: check emergency stop after health results
    health_results = _health_monitor.check_all()
    if _rule_engine.emergency_stop(health_results):
        log.warning('Rule 6: emergency stop active — health < %.1f, no new opens today', _rule_engine.HEALTH_STOP)

    # 1. Withdraw expired positions
    for pos in state.get_expired():
        if len(failed_today) >= MAX_DAILY_FAILURES:
            log.warning(f'Daily failure limit ({MAX_DAILY_FAILURES}) reached — stopping')
            return

        pos_id, platform, token, amount_wei, entry, expiry, tx_hash, *_rest = pos
        if platform not in CFG['platforms']:
            log.warning(f'Unknown platform {platform} in state, skipping')
            continue

        p     = CFG['platforms'][platform]
        ptype = p.get('type', '')
        log.info(f'Withdrawing expired {platform} {token} (due {expiry})')

        if ptype == 'compound_borrow':
            try:
                txh = _compound_borrow.close_borrow(str(amount_wei), p)
                state.close_position(pos_id)
                state.record_cooldown(platform)
                log.info(f'Closed {_pname(platform, p)} -> {txh}')
            except Exception as e:
                log.error(f'Close failed {_pname(platform, p)}: {e}')
                failed_today.append(f'close_borrow_{platform}')
            continue

        if ptype == 'mw_borrow':
            try:
                _mw_borrow.close_borrow(str(amount_wei), p, pos_id)
                state.record_cooldown(platform)
                log.info(f'Closed {_pname(platform, p)}')
            except Exception as e:
                log.error(f'Close failed {_pname(platform, p)}: {e}')
                failed_today.append(f'close_borrow_{platform}')
            continue

        if ptype == 'fluid_borrow':
            try:
                txh = _fl_borrow.close_borrow(str(amount_wei), p)
                state.close_position(pos_id)
                state.record_cooldown(platform)
                log.info(f'Closed {_pname(platform, p)} -> {txh}')
            except Exception as e:
                log.error(f'Close failed {_pname(platform, p)}: {e}')
                failed_today.append(f'close_borrow_{platform}')
            continue

        if ptype == 'aave_borrow':
            try:
                txh = _aave_borrow.close_borrow(str(amount_wei), p)
                state.close_position(pos_id)
                state.record_cooldown(platform)
                log.info(f'Closed {_pname(platform, p)} -> {txh}')
            except Exception as e:
                log.error(f'Close failed {_pname(platform, p)}: {e}')
                failed_today.append(f'close_borrow_{platform}')
            continue

        amt_int = int(float(amount_wei))
        try:
            txh = _withdraw(platform, amt_int)
            state.close_position(pos_id)
            state.record_cooldown(platform)
            log.info(f'Withdrew {platform} -> {txh}')
        except Exception as e:
            log.error(f'Withdraw failed {platform}: {e}')
            failed_today.append(f'withdraw_{platform}')
            continue

        tok_addr = p.get('token_address', USDC_ADDR)
        _return_to_eth_safe(p, tok_addr, amt_int, failed_today)

    # 2. Open new positions — skip entirely if emergency stop
    if _rule_engine.emergency_stop(health_results):
        log.warning('Rule 6: emergency stop — skipping new opens')
        log.info('=== daily job done ===')
        return

    active_positions  = state.get_active()
    active_set        = {p[1] for p in active_positions}
    n_actions         = _rule_engine.pick_action_count()
    spread_delays     = _rule_engine.pick_spread_delays(n_actions)

    candidates = _rule_engine.filter_candidates(
        ACTIVE_PLATFORMS,
        active_set,
        set(),
        CFG['platforms'],
        active_positions,
        health_results,
    )
    random.shuffle(candidates)

    # Rule 8: deduplicate to_open by protocol — no same protocol twice in one day
    today_opened_protocols: set = set()
    to_open = []
    for pk in candidates:
        if len(to_open) >= n_actions:
            break
        proto = _rule_engine.get_protocol(pk, CFG['platforms'].get(pk, {}))
        if proto not in today_opened_protocols:
            today_opened_protocols.add(proto)
            to_open.append(pk)
    today_opened_protocols.clear()  # reset — will be re-populated during actual opens

    for i, platform in enumerate(to_open):
        if len(failed_today) >= MAX_DAILY_FAILURES:
            log.warning(f'Daily failure limit ({MAX_DAILY_FAILURES}) reached — stopping')
            return

        if i > 0 and spread_delays:
            delay = spread_delays[i - 1]
            log.info(f'Rule 14: spread delay {delay}s before action {i+1}')
            time.sleep(delay)

        executor._local_nonce = None  # Rule 15: nonce reset before each action

        p           = CFG['platforms'][platform]
        expiry_days = _expiry_for(platform, p)
        protocol    = _rule_engine.get_protocol(platform, p)

        if p['type'] == 'compound_borrow':
            log.info(f'Opening {_pname(platform, p)} expiry={expiry_days}d')
            try:
                status = _compound_borrow.check_availability(
                    executor.Web3.to_checksum_address(p['comet_address']),
                    float(p.get('max_utilization', 0.90))
                )
                if not status['available']:
                    log.info(f'{_pname(platform, p)}: skip — util={status["utilization"]:.1%}')
                    continue
                encoded, txh = _compound_borrow.open_borrow(p, collateral_usd=collateral_usd)
                state.add_position(platform, p.get('borrow_token', 'USDC'), encoded, expiry_days, txh)
                today_opened_protocols.add(protocol)
                log.info(f'Opened {_pname(platform, p)} -> {txh}')
                state.log_daily_stat('borrow')
            except Exception as e:
                log.error(f'Open failed {_pname(platform, p)}: {e}')
                failed_today.append(f'supply_{platform}')
            continue

        if p['type'] == 'mw_borrow':
            log.info(f'Opening {_pname(platform, p)} expiry={expiry_days}d')
            try:
                avail = _mw_borrow.check_availability(p)
                if not avail['available']:
                    log.info(f'{_pname(platform, p)}: skip — util={avail["utilization"]:.1%}')
                    continue
                encoded = _mw_borrow.open_borrow(p, collateral_usd=collateral_usd)
                state.add_position(platform, p.get('borrow_token', 'USDC'), encoded, expiry_days, '')
                today_opened_protocols.add(protocol)
                log.info(f'Opened {_pname(platform, p)}')
                state.log_daily_stat('borrow')
            except Exception as e:
                log.error(f'Open failed {_pname(platform, p)}: {e}')
                failed_today.append(f'supply_{platform}')
            continue

        if p['type'] == 'fluid_borrow':
            log.info(f'Opening {_pname(platform, p)} expiry={expiry_days}d')
            try:
                encoded, txh = _fl_borrow.open_borrow(p, collateral_usd=collateral_usd)
                state.add_position(platform, p.get('borrow_token', 'USDC'), encoded, expiry_days, txh)
                today_opened_protocols.add(protocol)
                log.info(f'Opened {_pname(platform, p)} -> {txh}')
                state.log_daily_stat('borrow')
            except Exception as e:
                log.error(f'Open failed {_pname(platform, p)}: {e}')
                failed_today.append(f'supply_{platform}')
            continue

        if p['type'] == 'aave_borrow':
            log.info(f'Opening {_pname(platform, p)} expiry={expiry_days}d')
            try:
                encoded, txh = _aave_borrow.open_borrow(p, collateral_usd=collateral_usd)
                state.add_position(platform, p.get('borrow_token', 'USDC'), encoded, expiry_days, txh)
                today_opened_protocols.add(protocol)
                log.info(f'Opened {_pname(platform, p)} -> {txh}')
                state.log_daily_stat('borrow')
            except Exception as e:
                log.error(f'Open failed {_pname(platform, p)}: {e}')
                failed_today.append(f'supply_{platform}')
            continue

        if p['type'] in ('uni_lp', 'pancake_lp'):
            if not _prepare_token_safe(p, None, 0, failed_today):
                continue
            log.info(f'Opening {platform} ({p["type"]}) expiry={expiry_days}d')
            try:
                if p['type'] == 'uni_lp':
                    token_id, txh = _uni_lp.mint_uni_lp(platform)
                else:
                    token_id, txh = _pancake_lp.mint_pancake_lp(platform)
                state.add_position(platform, 'LP', str(token_id), expiry_days, txh)
                today_opened_protocols.add(protocol)
                log.info(f'Opened {platform} tokenId={token_id} -> {txh}')
                state.log_daily_stat('lp')
            except Exception as e:
                log.error(f'Supply failed {platform}: {e}')
                failed_today.append(f'supply_{platform}')
            continue

        amt      = _amount(platform)
        tok_addr = p.get('token_address', USDC_ADDR)

        if not _prepare_token_safe(p, tok_addr, amt, failed_today):
            continue

        log.info(f'Opening {platform} ({p["token"]}) amount={amt} expiry={expiry_days}d')
        try:
            txh = _supply(platform)
            state.add_position(platform, p['token'], amt, expiry_days, txh)
            today_opened_protocols.add(protocol)
            log.info(f'Opened {platform} -> {txh}')
            state.log_daily_stat('lp' if p['type'] == 'aero_lp' else 'lend')
        except Exception as e:
            log.error(f'Supply failed {platform}: {e}')
            failed_today.append(f'supply_{platform}')

    if failed_today:
        log.warning(f'Failures today: {failed_today}')

    # Portfolio snapshot (best-effort — RPC error must not fail the job)
    try:
        _portfolio_tracker.snapshot()
    except Exception as e:
        log.warning(f'portfolio_tracker failed: {e}')

    # Weekly report every Monday
    try:
        _weekly_report.run()
    except Exception as e:
        log.warning(f'weekly_report failed: {e}')

    log.info('=== daily job done ===')

_THE_RULE_MAX_RETRIES = 5
_CACHE_DIR      = os.path.join(os.path.dirname(__file__), 'cache')
_exec_lock      = threading.Lock()   # one wallet executes at a time
_RULE_LOG_MAX   = 100

def _get_rule_log_file() -> str:
    wid = os.environ.get('WALLET_ID', 'default')
    return os.path.join(_CACHE_DIR, f'rule_log_{wid}.json')
_ACTION_LOG_MAX  = 200


def _action_log(platform: str, step: str, detail: str,
                txhash: str | None = None, usd_est: float | None = None):
    """Set step_logger context and append action event to action_log.json."""
    import step_logger as _sl
    dn = CFG['platforms'].get(platform, {}).get('display_name', platform)
    _sl.set_context(platform, dn)
    _sl.slog(step, detail, txhash=txhash, usd_est=usd_est)


def _rule_log(original: str, current: str, attempt: int, ok: bool, reason: str,
              outcome: str | None = None, context: str = 'action'):
    """Append a The Rule validation event to rule_log.json."""
    from datetime import datetime as _dt
    entry = {
        'ts':       _dt.now().strftime('%H:%M:%S'),
        'date':     date.today().isoformat(),
        'context':  context,   # 'plan' | 'action' | 'maintenance' | 'close'
        'original': original,
        'current':  current,
        'attempt':  attempt,
        'ok':       ok,
        'reason':   reason,
        'outcome':  outcome,   # 'executed' | 'replaced' | 'skipped' | 'allowed' | 'blocked'
    }
    try:
        _rlf = _get_rule_log_file()
        os.makedirs(_CACHE_DIR, exist_ok=True)
        try:
            with open(_rlf) as f:
                entries = json.load(f)
        except Exception:
            entries = []
        entries.append(entry)
        entries = entries[-_RULE_LOG_MAX:]  # keep last N
        with open(_rlf, 'w') as f:
            json.dump(entries, f, indent=2)
    except Exception as e:
        log.warning(f'_rule_log write failed: {e}')


def _the_rule_repick(exclude: set) -> str | None:
    """
    THE RULE: find a replacement platform that passes all rules.
    exclude = set of platform keys already tried/rejected.
    Returns platform_key or None if nothing valid found.
    """
    state.init_db()
    active     = state.get_active()
    active_set = {p[1] for p in active}
    health_res = []
    try:
        health_res = _health_monitor.check_all()
    except Exception:
        pass

    all_p = [k for k, v in CFG['platforms'].items()
             if isinstance(v, dict)
             and v.get('type') not in ('aero_vote',)
             and k not in exclude]

    candidates = _rule_engine.filter_candidates(
        all_p, active_set, set(), CFG['platforms'], active, health_res
    )
    random.shuffle(candidates)

    eth = executor.get_eth_balance()
    today_opened = _today_opened_protocols()

    for pk in candidates:
        ok, _ = _rule_engine.pre_action_validate(
            pk, CFG['platforms'], active, health_res, today_opened, eth
        )
        if ok:
            return pk
    return None


def _today_opened_protocols() -> set:
    """Protocols already opened today (from state.db entries dated today)."""
    today = date.today().isoformat()
    protos = set()
    for pos in state.all_positions():
        if pos[4] == today and pos[7] == 'active':
            p_cfg = CFG['platforms'].get(pos[1], {})
            protos.add(_rule_engine.get_protocol(pos[1], p_cfg))
    return protos


def _open_platform(platform_key: str, collateral_usd: float = 0.0) -> bool:
    """
    THE RULE gate + open a single platform position.
    Returns True on success, False on failure.
    collateral_usd: if >0, passed to borrow open_borrow() to scale collateral from live price.
    """
    state.init_db()
    tried   = {platform_key}
    current = platform_key

    for attempt in range(_THE_RULE_MAX_RETRIES + 1):
        active     = state.get_active()
        eth        = executor.get_eth_balance()
        health_res = []
        try:
            health_res = _health_monitor.check_all()
        except Exception:
            pass
        today_opened = _today_opened_protocols()

        ok, reason = _rule_engine.pre_action_validate(
            current, CFG['platforms'], active, health_res, today_opened, eth
        )

        if ok:
            outcome = 'executed' if current == platform_key else 'replaced'
            _rule_log(platform_key, current, attempt + 1, True, reason, outcome)
            if current != platform_key:
                log.info(f'THE RULE: replaced {platform_key} -> {current} (attempt {attempt+1})')
            break

        _rule_log(platform_key, current, attempt + 1, False, reason)
        log.warning(f'THE RULE: rejected {current!r} — {reason}')

        replacement = _the_rule_repick(exclude=tried)
        if replacement is None:
            _rule_log(platform_key, current, attempt + 1, False, 'no replacement found', 'skipped')
            log.warning(f'THE RULE: no valid replacement found after {attempt+1} attempt(s) — skip')
            return False
        tried.add(replacement)
        current = replacement
    else:
        _rule_log(platform_key, current, _THE_RULE_MAX_RETRIES, False, 'max retries reached', 'skipped')
        log.warning(f'THE RULE: max retries ({_THE_RULE_MAX_RETRIES}) reached — skip')
        return False

    executor._local_nonce = None  # fresh nonce per action

    if platform_key not in CFG['platforms']:
        log.error(f'_open_platform: unknown platform {platform_key}')
        return False

    p           = CFG['platforms'][platform_key]
    expiry_days = _EXPIRY_OVERRIDE.pop(platform_key, None) or _expiry_for(platform_key, p)
    failed      = []

    log.info(f'=== open_platform {platform_key} expiry={expiry_days}d ===')

    if p['type'] == 'compound_borrow':
        try:
            log.info(f'[1] Check utilization {_pname(platform_key, p)} ...')
            status = _compound_borrow.check_availability(
                executor.Web3.to_checksum_address(p['comet_address']),
                float(p.get('max_utilization', 0.90))
            )
            if not status['available']:
                log.info(f'[SKIP] util={status["utilization"]:.1%} — too high')
                return False
            log.info(f'[2] Open borrow position ...')
            encoded, txh = _compound_borrow.open_borrow(p, collateral_usd=collateral_usd)
            state.add_position(platform_key, p.get('borrow_token','USDC'), encoded, expiry_days, txh)
            state.log_daily_stat('borrow')
            log.info(f'[OK] Opened {_pname(platform_key, p)} -> {txh}')
            _action_log(platform_key, 'ok', f'borrow open | TX {txh[:10]}...', txhash=txh)
            return True
        except Exception as e:
            log.error(f'[FAIL] compound_borrow [{platform_key}]: {e}')
            _action_log(platform_key, 'fail', str(e)[:100])
            return False

    if p['type'] == 'mw_borrow':
        try:
            log.info(f'[1] Check availability {_pname(platform_key, p)} ...')
            avail = _mw_borrow.check_availability(p)
            if not avail['available']:
                log.info(f'[SKIP] util={avail["utilization"]:.1%} — too high')
                return False
            log.info(f'[2] Open borrow position ...')
            encoded = _mw_borrow.open_borrow(p, collateral_usd=collateral_usd)
            state.add_position(platform_key, p.get('borrow_token','USDC'), encoded, expiry_days, '')
            state.log_daily_stat('borrow')
            log.info(f'[OK] Opened {_pname(platform_key, p)}')
            _action_log(platform_key, 'ok', 'borrow open')
            return True
        except Exception as e:
            log.error(f'[FAIL] mw_borrow [{platform_key}]: {e}')
            _action_log(platform_key, 'fail', str(e)[:100])
            return False

    if p['type'] == 'fluid_borrow':
        try:
            log.info(f'[1] Open Fluid borrow {_pname(platform_key, p)} ...')
            encoded, txh = _fl_borrow.open_borrow(p, collateral_usd=collateral_usd)
            state.add_position(platform_key, p.get('borrow_token','USDC'), encoded, expiry_days, txh)
            state.log_daily_stat('borrow')
            log.info(f'[OK] Opened {_pname(platform_key, p)} -> {txh}')
            _action_log(platform_key, 'ok', f'borrow open | TX {txh[:10]}...', txhash=txh)
            return True
        except Exception as e:
            log.error(f'[FAIL] fluid_borrow [{platform_key}]: {e}')
            _action_log(platform_key, 'fail', str(e)[:100])
            return False

    if p['type'] == 'aave_borrow':
        try:
            log.info(f'[1] Open AAVE borrow {_pname(platform_key, p)} ...')
            encoded, txh = _aave_borrow.open_borrow(p, collateral_usd=collateral_usd)
            state.add_position(platform_key, p.get('borrow_token','USDC'), encoded, expiry_days, txh)
            state.log_daily_stat('borrow')
            log.info(f'[OK] Opened {_pname(platform_key, p)} -> {txh}')
            _action_log(platform_key, 'ok', f'borrow open | TX {txh[:10]}...', txhash=txh)
            return True
        except Exception as e:
            log.error(f'[FAIL] aave_borrow [{platform_key}]: {e}')
            _action_log(platform_key, 'fail', str(e)[:100])
            return False

    if p['type'] in ('uni_lp', 'pancake_lp'):
        log.info(f'[1] Acquire tokens for {platform_key} ...')
        if not _prepare_token_safe(p, None, 0, failed):
            log.error(f'[FAIL] token acquisition failed: {failed}')
            return False
        try:
            log.info(f'[2] Mint LP position ...')
            if p['type'] == 'uni_lp':
                token_id, txh = _uni_lp.mint_uni_lp(platform_key)
            else:
                token_id, txh = _pancake_lp.mint_pancake_lp(platform_key)
            state.add_position(platform_key, 'LP', str(token_id), expiry_days, txh)
            state.log_daily_stat('lp')
            log.info(f'[OK] Opened {platform_key} tokenId={token_id} -> {txh}')
            _action_log(platform_key, 'ok', f'LP tokenId={token_id} | TX {txh[:10]}...', txhash=txh)
            return True
        except Exception as e:
            log.error(f'[FAIL] mint LP [{platform_key}]: {e}')
            _action_log(platform_key, 'fail', str(e)[:100])
            return False

    # All other supply/lend/aero_lp types — with repick on execution failure
    MAX_EXEC = 3
    for exec_attempt in range(1, MAX_EXEC + 1):
        amt      = _amount(current)
        tok_addr = CFG['platforms'][current].get('token_address', USDC_ADDR)
        p_cur    = CFG['platforms'][current]
        failed   = []
        log.info(f'[{exec_attempt}/{MAX_EXEC}] Acquire tokens for {current} ...')
        if not _prepare_token_safe(p_cur, tok_addr, amt, failed):
            log.error(f'[{exec_attempt}/{MAX_EXEC}] FAIL token acquisition: {failed}')
            replacement = _the_rule_repick(exclude=tried)
            if replacement is None:
                log.warning(f'[{exec_attempt}/{MAX_EXEC}] No repick available — giving up')
                return False
            tried.add(replacement)
            current = replacement
            log.info(f'[REPICK] -> {current}')
            _action_log(platform_key, 'repick', f'-> {current}')
            continue
        # Extra settle time for erc4626 vaults: stale RPC may show pre-swap balance
        if CFG['platforms'][current].get('type') == 'erc4626':
            import time as _t; _t.sleep(4)
        try:
            log.info(f'[{exec_attempt}/{MAX_EXEC}] Supply/deposit to {current} ...')
            txh = _supply(current)
            expiry_days = _expiry_for(current, p_cur)
            state.add_position(current, p_cur.get('token', ''), amt, expiry_days, txh)
            state.log_daily_stat('lp' if p_cur['type'] == 'aero_lp' else 'lend')
            log.info(f'[OK] Opened {current} -> {txh}')
            _action_log(current, 'ok', f'supply | TX {txh[:10]}...', txhash=txh)
            return True
        except Exception as e:
            log.error(f'[{exec_attempt}/{MAX_EXEC}] FAIL supply [{current}]: {e}')
            _action_log(current, 'fail', str(e)[:100])
            # Sweep residual tokens (e.g. cbBTC from failed deposit) before trying next platform
            try:
                import sweep_tokens as _sw
                _sw.run()
            except Exception as _se:
                log.warning(f'sweep after supply fail: {_se}')
            if exec_attempt >= MAX_EXEC:
                log.warning(f'[FAIL] Max attempts ({MAX_EXEC}) reached for {platform_key}')
                return False
            replacement = _the_rule_repick(exclude=tried)
            if replacement is None:
                log.warning(f'No repick available — giving up')
                return False
            tried.add(replacement)
            current = replacement
            log.info(f'[REPICK] -> {current}')
            _action_log(platform_key, 'repick', f'-> {current}')
    return False


def _mark_plan_done(platform_key: str):
    """Mark plan entry as done in per-wallet plan file."""
    try:
        from daily_briefing import get_plan_file
        import json as _json
        _pf = get_plan_file()
        with open(_pf) as f:
            plan_data = _json.load(f)
        for a in plan_data.get('actions', []):
            if a['platform'] == platform_key:
                a['done'] = True
        with open(_pf, 'w') as f:
            _json.dump(plan_data, f, indent=2)
    except Exception:
        pass


def _open_platform_with_recovery(platform_key: str, wallet_id: str = None):
    """
    Wrapper around _open_platform.
    Switches to wallet_id context, acquires exec lock, executes.
    On failure: sweep tokens → ETH, repick new platform, reschedule +5min.
    """
    wid = wallet_id or os.environ.get('WALLET_ID', 'default')

    with _exec_lock:
        # Switch to target wallet context (reloads executor + state in-place)
        _wm.switch_context(wid)
        for _mod in ('executor', 'state'):
            if _mod in sys.modules:
                importlib.reload(sys.modules[_mod])
        state.init_db()
        executor._local_nonce = None
        log.info(f'[{wid}] === executing {platform_key} ===')

        _open_platform_with_recovery_inner(platform_key, wid)


def _open_platform_with_recovery_inner(platform_key: str, wid: str):
    """Actual execution — called inside _exec_lock with correct wallet context."""
    # Set amount + expiry overrides from plan
    _usd_override = 0.0
    try:
        from daily_briefing import load_plan
        plan = load_plan()
        for a in (plan or []):
            if a['platform'] == platform_key and not a.get('done', False):
                usd = float(a.get('usd_est', 0))
                if usd > 0:
                    _usd_override = usd
                    _AMOUNT_OVERRIDE[platform_key] = _amount_from_usd(platform_key, usd)
                    log.info(f'Amount override: {platform_key} = ${usd:.2f} -> {_AMOUNT_OVERRIDE[platform_key]} wei')
                exp = a.get('expiry_days')
                if exp:
                    _EXPIRY_OVERRIDE[platform_key] = int(exp)
                    log.info(f'Expiry override: {platform_key} = {exp}d (from plan)')
                break
    except Exception as e:
        log.warning(f'Could not set overrides: {e}')

    _action_log(platform_key, 'start',
                f'${_usd_override:.2f}' if _usd_override > 0 else 'default amount',
                usd_est=_usd_override if _usd_override > 0 else None)

    # Override LP budgets too
    global AERO_LP_BUDGET_USD, UNI_LP_BUDGET_USD, PANCAKE_LP_BUDGET_USD, BEEFY_LP_BUDGET_USD
    _orig_aero, _orig_uni, _orig_cake, _orig_beefy = (
        AERO_LP_BUDGET_USD, UNI_LP_BUDGET_USD, PANCAKE_LP_BUDGET_USD, BEEFY_LP_BUDGET_USD
    )
    if _usd_override > 0:
        AERO_LP_BUDGET_USD    = _usd_override
        UNI_LP_BUDGET_USD     = _usd_override
        PANCAKE_LP_BUDGET_USD = _usd_override
        BEEFY_LP_BUDGET_USD   = _usd_override

    # Mark done BEFORE execution so plan_sync_job (60s interval) sees done=True
    # immediately and cannot re-schedule this action while it is running.
    # If execution fails, recovery appends a NEW entry for the replacement.
    _mark_plan_done(platform_key)

    success = _open_platform(platform_key, collateral_usd=_usd_override)

    # Restore overrides
    _AMOUNT_OVERRIDE.pop(platform_key, None)
    AERO_LP_BUDGET_USD    = _orig_aero
    UNI_LP_BUDGET_USD     = _orig_uni
    PANCAKE_LP_BUDGET_USD = _orig_cake
    BEEFY_LP_BUDGET_USD   = _orig_beefy

    if success:
        return

    # ── RECOVERY ────────────────────────────────────────────────
    log.warning(f'--- RECOVERY: {platform_key} failed — sweeping tokens -> ETH ---')
    _rule_log(platform_key, platform_key, 0, False, 'action failed — starting recovery', 'failed', context='recovery')
    _action_log(platform_key, 'recovery', 'failed — sweeping tokens -> ETH')

    try:
        import sweep_tokens
        sweep_tokens.run()
        log.info('--- RECOVERY: sweep complete ---')
        _rule_log(platform_key, platform_key, 0, True, 'tokens swept -> ETH', 'swept', context='recovery')
        _action_log(platform_key, 'recovery', 'sweep complete -> ETH')
    except Exception as e:
        log.error(f'--- RECOVERY: sweep error: {e} ---')
        _rule_log(platform_key, platform_key, 0, False, f'sweep error: {e}', 'sweep_failed', context='recovery')
        _action_log(platform_key, 'recovery', f'sweep error: {str(e)[:60]}')

    log.info('--- RECOVERY: repick new platform ---')
    tried = {platform_key}
    replacement = _the_rule_repick(exclude=tried)
    if replacement is None:
        log.warning('--- RECOVERY: no valid replacement found — skip ---')
        _rule_log(platform_key, platform_key, 0, False, 'no replacement found after recovery', 'skipped', context='recovery')
        _action_log(platform_key, 'recovery', 'no valid replacement found — skip')
        return

    log.info(f'--- RECOVERY: selected {replacement} ---')
    _rule_log(platform_key, replacement, 0, True, 'repick after recovery', 'replaced', context='recovery')
    _action_log(platform_key, 'recovery', f'repick -> {replacement}')

    # Build a minimal plan entry for the replacement
    p_cfg = CFG['platforms'].get(replacement, {})
    ptype = p_cfg.get('type', '')
    if 'borrow' in ptype:
        disp_type = 'BORROW'
    elif 'lp' in ptype:
        disp_type = 'LP'
    else:
        disp_type = 'LEND'

    now_utc  = datetime.utcnow()
    run_at   = now_utc + timedelta(minutes=5)
    bkk_time = (run_at + timedelta(hours=7)).strftime('%H:%M')

    try:
        repl_usd_est = _rule_engine.pick_amount_usd()
    except Exception:
        repl_usd_est = 5.0
    try:
        repl_expiry = _settings.expiry_for_type(ptype)
    except Exception:
        repl_expiry = random.randint(*p_cfg.get('expiry_days', [3, 5]))

    # Read current plan to get next unique idx
    try:
        from daily_briefing import get_plan_file as _gpf
        import json as _j2
        with open(_gpf()) as _fp:
            _pd = _j2.load(_fp)
        _next_idx = max((a.get('idx', 0) for a in _pd.get('actions', [])), default=0) + 1
    except Exception:
        _next_idx = int(datetime.utcnow().timestamp()) % 100000  # fallback: epoch-based

    new_action = {
        'idx':          _next_idx,
        'platform':     replacement,
        'display_name': p_cfg.get('display_name', replacement),
        'type':         ptype,
        'disp_type':    disp_type,
        'token':        p_cfg.get('token') or p_cfg.get('borrow_token', ''),
        'usd_est':      repl_usd_est,
        'expiry_days':  repl_expiry,
        'time_bkk':     bkk_time,
        'run_at_utc':   run_at.isoformat(),
        'date':         date.today().isoformat(),
        'done':         False,
    }

    # Append to plan file
    try:
        from daily_briefing import get_plan_file
        import json as _json
        _pf = get_plan_file()
        with open(_pf) as f:
            plan_data = _json.load(f)
        plan_data['actions'].append(new_action)
        with open(_pf, 'w') as f:
            _json.dump(plan_data, f, indent=2)
        log.info(f'--- RECOVERY: {replacement} scheduled @ {bkk_time} BKK (+5min) ---')
        _rule_log(platform_key, replacement, 0, True, f'rescheduled @ {bkk_time} BKK', 'rescheduled', context='recovery')
        _action_log(replacement, 'recovery', f'rescheduled @ {bkk_time} BKK (+5min)')
    except Exception as e:
        log.error(f'--- RECOVERY: failed to write plan: {e} ---')
        return

    # Schedule immediately via APScheduler (wallet_id from env — already switched)
    _schedule_action(new_action, wid)


def _schedule_wallet_actions(wallet_id: str, actions: list, stale_aware: bool = False):
    """Schedule all TODO actions for one wallet. Handles stale-spreading per-wallet."""
    now_utc    = datetime.utcnow()
    _today     = now_utc.date()
    CUTOFF_UTC = datetime(_today.year, _today.month, _today.day, 16, 50)  # 23:50 BKK
    stale_count  = 0
    plan_updated = False

    for a in actions:
        if a.get('done', False):
            continue
        run_at = datetime.fromisoformat(a['run_at_utc'])
        if stale_aware and run_at <= now_utc:
            probe_count = stale_count + 1
            # Stale gap: 3min apart so actions don't collide if bot was down a long time
            new_run_utc = now_utc + timedelta(minutes=probe_count * 3)
            if new_run_utc > CUTOFF_UTC:
                log.info(f'[{wallet_id}] Stale {a["platform"]} skipped — past 23:50 BKK cutoff')
                continue
            stale_count = probe_count
            new_bkk     = new_run_utc + timedelta(hours=7)
            a['run_at_utc'] = new_run_utc.isoformat()
            a['time_bkk']   = f'{new_bkk.hour:02d}:{new_bkk.minute:02d}'
            plan_updated     = True
            log.info(f'[{wallet_id}] Stale {a["platform"]} → {a["time_bkk"]} BKK (overdue, +{probe_count*3}min)')
        _schedule_action(a, wallet_id)

    if plan_updated:
        try:
            plan_file = os.path.join(_CACHE_DIR, f'plan_{wallet_id}.json')
            with open(plan_file) as _f:
                _pd = json.load(_f)
            _pd['actions'] = actions
            with open(plan_file, 'w') as _f:
                json.dump(_pd, _f, indent=2)
        except Exception as _e:
            log.warning(f'[{wallet_id}] Failed to persist stale-spread plan: {_e}')


def _schedule_action(a: dict, wallet_id: str = None):
    """Schedule a single plan action as a one-shot APScheduler job."""
    if _scheduler is None:
        return
    wid    = wallet_id or os.environ.get('WALLET_ID', 'default')
    job_id = f'plan_{wid}_{a["platform"]}'
    now_utc = datetime.utcnow()
    run_at  = datetime.fromisoformat(a['run_at_utc'])
    if run_at <= now_utc:
        run_at = now_utc + timedelta(minutes=2)
        log.info(f'[{wid}] {a["platform"]} time passed — rescheduled +2min')
    _scheduler.add_job(
        _open_platform_with_recovery,
        'date',
        run_date=run_at,
        args=[a['platform'], wid],
        id=job_id,
        replace_existing=True,
    )
    log.info(f'[{wid}] Scheduled: {a["display_name"]}  {a["time_bkk"]} BKK  [{a["disp_type"]}]')


def _print_all_wallet_briefings():
    """Print briefing cards for all wallets. Full card if active, minimal card if inactive."""
    from daily_briefing import print_card, build, print_inactive_card as _pic
    print_card(build())
    _primary_wid = os.environ.get('WALLET_ID', '')
    for _w_other in _wm.load_wallets():
        if _w_other['id'] == _primary_wid:
            continue
        if _w_other.get('active', False):
            try:
                _wm.switch_context(_w_other['id'])
                importlib.reload(sys.modules['state'])
                print_card(build())
            except Exception as _we:
                log.warning(f'[{_w_other["id"]}] briefing failed: {_we}')
            finally:
                _wm.switch_context(_primary_wid)
                importlib.reload(sys.modules['state'])
        else:
            _pic(_w_other)


def briefing_and_plan():
    """
    07:00 BKK (00:00 UTC):
    - If today's plan already exists (e.g. restart/reboot): reload + re-schedule TODO items
    - Otherwise: generate new plan
    Then print card.
    """
    global _scheduler
    try:
        from daily_briefing import load_plan, build, print_card, get_plan_file
        import json as _json
        today = date.today().isoformat()

        # Determine if any wallet has today's plan already (e.g. restart)
        existing = load_plan()   # current wallet (primary)
        if existing and existing[0].get('date') == today:
            log.info(f'briefing_and_plan: plans exist — reschedule only')
        else:
            # Dispatcher: generate plans for ALL wallets (no-overlap assignment)
            try:
                import dispatcher as _disp
                _disp.plan_all_wallets()
                log.info('briefing_and_plan: dispatcher done — all wallets planned')
            except Exception as _de:
                log.warning(f'briefing_and_plan: dispatcher failed ({_de}), falling back to plan_day()')
                from daily_briefing import plan_day
                plan_day()

        # Schedule actions for ALL active wallets
        for _w in _wm.load_wallets():
            _wid = _w['id']
            if not _w.get('active', True):
                log.info(f'[{_wid}] skipped (active=false)')
                continue
            _plan_path = os.path.join(_CACHE_DIR, f'plan_{_wid}.json')
            try:
                with open(_plan_path) as _f:
                    _pd = _json.load(_f)
                if _pd.get('date') != today:
                    log.info(f'[{_wid}] plan stale — skip scheduling')
                    continue
                _actions = _pd.get('actions', [])
                _schedule_wallet_actions(_wid, _actions, stale_aware=True)
                log.info(f'[{_wid}] {sum(1 for a in _actions if not a.get("done"))} action(s) scheduled')
            except FileNotFoundError:
                log.info(f'[{_wid}] no plan file yet')
            except Exception as _se:
                log.warning(f'[{_wid}] scheduling failed: {_se}')

        _print_all_wallet_briefings()

    except Exception as e:
        log.warning(f'briefing_and_plan failed: {e}')


def plan_sync_job():
    """
    Runs every 60s — syncs APScheduler with ALL wallet plan files.
    Job IDs: plan_{wallet_id}_{platform}.
    """
    if _scheduler is None:
        return
    try:
        today     = date.today().isoformat()
        wallets   = _wm.load_wallets()
        valid_ids: set = set()
        wallet_actions: dict = {}   # wid -> list[action]

        # Collect valid job IDs from all active wallet plans
        for w in wallets:
            wid = w['id']
            if not w.get('active', True):
                continue
            plan_path = os.path.join(_CACHE_DIR, f'plan_{wid}.json')
            try:
                with open(plan_path) as f:
                    pd = json.load(f)
                if pd.get('date') != today:
                    continue
                actions = pd.get('actions', [])
                wallet_actions[wid] = actions
                for a in actions:
                    if not a.get('done', False):
                        valid_ids.add(f'plan_{wid}_{a["platform"]}')
            except FileNotFoundError:
                pass
            except Exception as e:
                log.warning(f'plan_sync: read {wid} plan failed: {e}')

        # Remove stale plan_* jobs no longer in any wallet plan
        changed = False
        for job in _scheduler.get_jobs():
            if job.id.startswith('plan_') and job.id not in valid_ids:
                log.info(f'plan_sync: removing stale job {job.id}')
                job.remove()
                changed = True

        # Add new TODO actions not yet scheduled (for all wallets)
        for wid, actions in wallet_actions.items():
            for a in actions:
                if a.get('done', False):
                    continue
                job_id = f'plan_{wid}_{a["platform"]}'
                if _scheduler.get_job(job_id) is None:
                    log.info(f'plan_sync: [{wid}] scheduling {a["platform"]} @ {a["time_bkk"]} BKK')
                    _schedule_action(a, wid)
                    changed = True

        # Reprint briefing cards for all wallets if plan changed
        if changed:
            try:
                _print_all_wallet_briefings()
            except Exception:
                pass

        # Wake-up catch-up: if past 07:00 BKK (00:00 UTC) and no wallet has today's plan
        # → auto-trigger briefing (handles sleep/resume + missed cron misfire)
        global _last_auto_brief_date, _last_maintenance_date
        now_utc = datetime.utcnow()
        day_start_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        if now_utc >= day_start_utc and _last_auto_brief_date != today:
            active_wallets = [w for w in wallets if w.get('active', True)]
            has_plan = any(w['id'] in wallet_actions for w in active_wallets)
            if not has_plan:
                _last_auto_brief_date = today
                log.info('plan_sync: wake-up catch-up — no plan for today, triggering briefing')
                threading.Thread(target=briefing_and_plan, daemon=True).start()

        # Wake-up catch-up: if past 07:05 BKK (00:05 UTC) and maintenance hasn't run today
        # → auto-trigger maintenance (withdraw expired + health check + periodic actions)
        maint_start_utc = now_utc.replace(hour=0, minute=5, second=0, microsecond=0)
        if now_utc >= maint_start_utc and _last_maintenance_date != today:
            _last_maintenance_date = today
            log.info('plan_sync: wake-up catch-up — maintenance not run today, triggering')
            threading.Thread(target=maintenance_job, daemon=True).start()

    except Exception as e:
        log.warning(f'plan_sync_job failed: {e}')


def _maintenance_single_wallet(wid: str, failed: list):
    """Run all maintenance tasks for one wallet. Called inside _exec_lock with context switched."""
    log.info(f'[{wid}] maintenance start')
    state.init_db()
    executor._local_nonce = None

    eth  = executor.get_eth_balance()
    usdc = executor.get_token_balance(USDC_ADDR, decimals=6)
    log.info(f'[{wid}] ETH: {eth:.5f}  USDC: {usdc:.2f}')

    health_results = _health_monitor.check_all()
    ok, reason = _rule_engine.validate_maintenance_entry(eth, health_results)
    _rule_log('maintenance_job', 'maintenance_job', 1, ok, reason,
              outcome='allowed' if ok else 'blocked', context='maintenance')
    if not ok:
        log.warning(f'[{wid}] THE RULE maintenance: BLOCKED — {reason}')
        return
    log.info(f'[{wid}] THE RULE maintenance: allowed')

    _check_borrow_health(failed)
    _run_periodic_actions(failed)

    for pos in state.get_expired():
        pos_id, platform, token, amount_wei, entry, expiry, tx_hash, *_rest = pos
        ok_c, reason_c = _rule_engine.validate_close_entry(pos_id, platform, CFG['platforms'])
        _rule_log(f'close#{pos_id}:{platform}', f'close#{pos_id}:{platform}', 1,
                  ok_c, reason_c,
                  outcome='allowed' if ok_c else 'blocked', context='close')
        if not ok_c:
            log.warning(f'[{wid}] BLOCKED close pos#{pos_id} {platform} — {reason_c}')
            continue
        log.info(f'[{wid}] Withdrawing expired [{pos_id}] {platform} (expired {expiry})')
        _action_log(platform, 'close', f'expired {expiry} — withdrawing')
        try:
            import withdraw_all as _wa
            _wa.run(positions_override=[pos])
        except Exception as e:
            log.error(f'[{wid}] Expire-withdraw failed {platform}: {e}')

    try:
        _portfolio_tracker.snapshot()
    except Exception as e:
        log.warning(f'[{wid}] portfolio_tracker failed: {e}')

    log.info(f'[{wid}] maintenance done')


def maintenance_job():
    """
    07:05 BKK (00:05 UTC) — iterate all active wallets, run maintenance for each.
    Weekly report runs once (primary wallet context).
    """
    global _last_maintenance_date
    _last_maintenance_date = date.today().isoformat()
    log.info('=== maintenance job start ===')
    failed_today: list = []

    for w in _wm.load_wallets():
        wid = w['id']
        if not w.get('active', True) or not w.get('private_key', ''):
            log.info(f'[{wid}] skipped (inactive or no key)')
            continue
        with _exec_lock:
            _wm.switch_context(wid)
            for _mod in ('executor', 'state'):
                if _mod in sys.modules:
                    importlib.reload(sys.modules[_mod])
            try:
                _maintenance_single_wallet(wid, failed_today)
            except Exception as e:
                log.error(f'[{wid}] maintenance error: {e}')

    # Weekly report once (primary wallet context restored by last switch)
    try:
        _weekly_report.run()
    except Exception as e:
        log.warning(f'weekly_report failed: {e}')

    if failed_today:
        log.warning(f'Maintenance failures: {failed_today}')
    log.info('=== maintenance job done ===')


_scheduler = None
_last_auto_brief_date:    str | None = None   # guard: auto catch-up once per day
_last_maintenance_date:   str | None = None   # guard: maintenance catch-up once per day

if __name__ == '__main__':
    # ── Startup: set WALLET_ID so cache files are per-wallet ─────────────
    # (_wm already imported at module level)
    _wid = _wm.get_id_for_address(os.environ.get('WALLET_ADDRESS', ''))
    if _wid:
        os.environ['WALLET_ID'] = _wid
        _w = _wm.get_wallet(_wid)
        if _w and 'state_db' in _w:
            os.environ['STATE_DB_PATH'] = os.path.join(os.path.dirname(__file__), _w['state_db'])
        log.info(f'Wallet context: {_wid} ({os.environ.get("WALLET_ADDRESS", "")})')
    else:
        log.warning('WALLET_ADDRESS not found in wallets.json — cache files use id=default')

    # ── Startup: backup + on-chain reconcile for ALL wallets ─────────────
    import onchain_recovery as _onchain
    log.info('On-chain reconcile: all wallets...')
    for _w_entry in _wm.load_wallets():
        _wid_r = _w_entry['id']
        try:
            _wm.switch_context(_wid_r)       # sets env + reloads executor/state
            importlib.reload(sys.modules.get('onchain_recovery', _onchain))
            state.init_db()
            try:
                _bp = state.backup_db()
                log.info(f'  {_wid_r}: DB backed up → {_bp}')
            except Exception as _be:
                log.warning(f'  {_wid_r}: backup failed: {_be}')
            _r = _onchain.reconcile(verbose=False)
            log.info(f'  {_wid_r}: +{_r["added"]} recovered, -{_r["closed"]} closed')
        except Exception as _re:
            log.error(f'  {_wid_r}: reconcile failed: {_re}')

    # Restore primary wallet context
    _wm.switch_context(_wid)
    importlib.reload(sys.modules.get('onchain_recovery', _onchain))
    state.init_db()

    log.info('Scheduler starting')
    log.info('  00:00 UTC (07:00 BKK) -> briefing + plan day')
    log.info('  00:05 UTC (07:05 BKK) -> maintenance (closes + health)')
    log.info('  Planned actions: random times 07:01-23:50 BKK')

    # Print wallet registry
    _wsep = '=' * 55
    print(_wsep)
    print('  WALLETS')
    print(_wsep)
    for _wl in _wm.load_wallets():
        _ck   = '[x]' if _wl.get('active') else '[ ]'
        _pk_s = 'PK=SET' if _wl.get('private_key') else 'PK=---'
        print(f'  {_ck} {_wl["id"]:<10}  {_wl["address"]}  {_pk_s}  ({_wl.get("state_db", "?")})')
    print(_wsep)

    _settings.print_config()

    _scheduler = BlockingScheduler(timezone='UTC')
    _scheduler.add_job(briefing_and_plan, 'cron',     hour=0, minute=0,
                       misfire_grace_time=86400)                              # 07:00 BKK — fire even after sleep/resume
    _scheduler.add_job(maintenance_job,   'cron',     hour=0, minute=5,
                       misfire_grace_time=86400)                              # 07:05 BKK — fire even after sleep/resume
    _scheduler.add_job(plan_sync_job,     'interval', seconds=60)              # every 60s

    # On startup: reload or generate today's plan + schedule actions
    briefing_and_plan()

    try:
        _scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info('Scheduler stopped')
