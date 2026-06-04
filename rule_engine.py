"""
rule_engine.py — Platform selection and daily scheduling rules (v2).

Implements rules 1-26 for Base airdrop agent daily_job().
Pure Python: no web3, no executor, no swap imports.
All functions take parameters explicitly — testable without mocks (except state).

Rules implemented:
  Selection  2-11 : candidate filtering (balance guard, emergency stop,
                    active check, protocol uniqueness, cooldown, caps, diversity)
  Amount     12   : tiered USD amount ($5-8 / $8-12 / $12-15)
  Timing     13-14: random start time + action spread delays
  Retention  24-26: USDC <= $10, WETH <= 0.005 thresholds for sweep
  (Rules 1, 4, 15-23 are handled in agent.py / health_monitor.py)
"""

import random, logging
from datetime import date, timedelta

import state as _state

log = logging.getLogger(__name__)

# Thresholds (module-level defaults — actual values read from settings at call-time)
ETH_MIN        = 0.005
HEALTH_STOP    = 1.1
MAX_CONCURRENT = {'lp': 5, 'lend': 6, 'borrow': 4}

# Token retention defaults (Rules 24-26)
USDC_RETAIN_USD = 10.0
WETH_RETAIN_ETH = 0.005
USDC_RETAIN_WEI = int(USDC_RETAIN_USD * 1e6)
WETH_RETAIN_WEI = int(WETH_RETAIN_ETH * 1e18)


def get_eth_min() -> float:
    """Return current ETH minimum threshold from settings (call-time read)."""
    try:
        import settings as _s
        return float(_s.load().get('eth_min', ETH_MIN))
    except Exception:
        return ETH_MIN


def get_max_concurrent() -> dict:
    """Return current max concurrent limits from settings (call-time read)."""
    try:
        import settings as _s
        return _s.load().get('max_concurrent', MAX_CONCURRENT)
    except Exception:
        return MAX_CONCURRENT

# Platform classification
BORROW_TYPES = {'compound_borrow', 'mw_borrow', 'fluid_borrow', 'aave_borrow'}
SUPPLY_TYPES = {'comet', 'erc4626', 'ctoken', 'psm_hold', 'beefy_single', 'aave_supply'}
LP_TYPES     = {'beefy_lp', 'aero_lp', 'uni_lp', 'pancake_lp'}


def _platform_category(ptype: str) -> str:
    if ptype in BORROW_TYPES: return 'borrow'
    if ptype in SUPPLY_TYPES: return 'lend'
    if ptype in LP_TYPES:     return 'lp'
    return 'other'


def get_protocol(platform_key: str, p_cfg: dict) -> str:
    """Rule 8: protocol family from platform key."""
    if 'protocol' in p_cfg:
        return p_cfg['protocol']
    k = platform_key.lower()
    if k.startswith('cb_'):        return 'compound'
    if k.startswith('compound'):   return 'compound'
    if k.startswith('mw_'):        return 'moonwell'
    if k.startswith('moonwell'):   return 'moonwell'
    if k.startswith('fl_'):        return 'fluid'
    if k.startswith('fluid'):      return 'fluid'
    if k.startswith('aave'):       return 'aave'
    if k.startswith('beefy'):      return 'beefy'
    if k.startswith('aero_lp'):    return 'aerodrome'
    if k.startswith('uni_lp'):     return 'uniswap'
    if k.startswith('pancake'):    return 'pancake'
    if k.startswith('morpho'):     return 'morpho'
    if k.startswith('spark'):      return 'spark'
    return platform_key.split('_')[0]


def balance_guard(eth_balance: float) -> bool:
    """Rule 5: True = safe to proceed."""
    return eth_balance >= get_eth_min()


def emergency_stop(health_results: list) -> bool:
    """Rule 6: True if any borrow health < HEALTH_STOP."""
    return any(
        r['health'] < HEALTH_STOP
        for r in health_results
        if r['status'] != 'ERROR'
    )


def pick_action_count() -> int:
    """Rule 3: 1-3 actions/day (40%/40%/20%)."""
    return random.choices([1, 2, 3], weights=[0.4, 0.4, 0.2])[0]


def pick_amount_usd() -> float:
    """Rule 12: tiered random USD — tiers and weights read from settings at call-time."""
    try:
        import settings as _s
        tiers = _s.load().get('usd_tiers', [
            {'label': 'low',  'min': 5.0,  'max': 8.0,  'weight': 0.70},
            {'label': 'mid',  'min': 8.0,  'max': 12.0, 'weight': 0.25},
            {'label': 'high', 'min': 12.0, 'max': 15.0, 'weight': 0.05},
        ])
        weights = [t['weight'] for t in tiers]
        tier = random.choices(tiers, weights=weights)[0]
        return round(random.uniform(float(tier['min']), float(tier['max'])), 2)
    except Exception:
        tier = random.choices(['low', 'mid', 'high'], weights=[0.70, 0.25, 0.05])[0]
        if tier == 'low': return round(random.uniform(5.0,  8.0),  2)
        if tier == 'mid': return round(random.uniform(8.0,  12.0), 2)
        return round(random.uniform(12.0, 15.0), 2)


def is_in_cooldown(platform_key: str) -> bool:
    """Rule 9: platform must wait 1 day after close."""
    return _state.get_cooldown_days(platform_key) < 1


def count_active_by_category(active_positions: list, platform_cfgs: dict) -> dict:
    """Count active positions per category for Rule 10 cap enforcement.
    Dust positions (opened_usd < $1) are excluded — they don't hold a slot."""
    counts = {'lend': 0, 'borrow': 0, 'lp': 0, 'other': 0}
    for pos in active_positions:
        opened_usd = pos[8] if len(pos) > 8 else None
        if opened_usd is not None and opened_usd < 1.0:
            continue
        ptype = platform_cfgs.get(pos[1], {}).get('type', '')
        counts[_platform_category(ptype)] += 1
    return counts


def categories_this_week(platform_cfgs: dict) -> set:
    """Rule 11: distinct action categories opened in last 7 days (excl. 'other')."""
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    cats = set()
    for pos in _state.all_positions():
        entry_date = pos[4]
        if entry_date >= week_ago:
            ptype = platform_cfgs.get(pos[1], {}).get('type', '')
            cats.add(_platform_category(ptype))
    return cats - {'other'}


def filter_candidates(
    all_platforms: list,
    active_set: set,
    today_opened_protocols: set,
    platform_cfgs: dict,
    active_positions: list,
    health_results: list,
) -> list:
    """
    Filter platform list to eligible candidates for opening today.
    Applies rules 6-10 (hard filters) + Rule 11 (soft ordering: prefer uncovered categories).

    Parameters:
      all_platforms           : full candidate list from contracts.json
      active_set              : platform keys currently active in state.db
      today_opened_protocols  : protocol strings already opened today
      platform_cfgs           : CFG['platforms'] dict
      active_positions        : state.get_active() rows
      health_results          : health_monitor.check_all() output
    """
    if emergency_stop(health_results):
        log.warning('Rule 6: emergency stop — no new opens (health < %.1f)', HEALTH_STOP)
        return []

    counts = count_active_by_category(active_positions, platform_cfgs)
    mc     = get_max_concurrent()
    eligible = []

    # Rule 12a: supply/borrow collision sets (AAVE + Moonwell share same mToken/aToken pool)
    # supply types that provide the collateral token on-chain
    _R12_SUPPLY = {'aave_supply', 'ctoken'}
    # borrow types that lock a collateral token on-chain
    _R12_BORROW = {'aave_borrow', 'mw_borrow'}
    active_borrow_coll_toks = set()   # collateral tokens locked by active borrows
    active_supply_toks      = set()   # tokens currently in standalone supply positions
    for pos in active_positions:
        acfg = platform_cfgs.get(pos[1], {})
        pt   = acfg.get('type', '')
        if pt in _R12_BORROW:
            active_borrow_coll_toks.add(acfg.get('collateral_token', ''))
        elif pt in _R12_SUPPLY:
            active_supply_toks.add(acfg.get('token', ''))

    for platform in all_platforms:
        p     = platform_cfgs.get(platform, {})
        ptype = p.get('type', '')
        cat   = _platform_category(ptype)

        if platform in active_set:                          # Rule 7
            continue
        protocol = get_protocol(platform, p)
        if protocol in today_opened_protocols:              # Rule 8
            continue
        if is_in_cooldown(platform):                       # Rule 9
            continue
        cap = mc.get(cat, 999)
        if counts.get(cat, 0) >= cap:                      # Rule 10
            continue

        # Rule 12a: supply/borrow collision — same token can't be both standalone supply & borrow collateral
        if ptype in _R12_SUPPLY and p.get('token', '') in active_borrow_coll_toks:
            continue
        if ptype in _R12_BORROW and p.get('collateral_token', '') in active_supply_toks:
            continue

        eligible.append(platform)

    # Rule 11: prefer categories not yet covered this week (soft ordering — not hard filter)
    try:
        week_cats = categories_this_week(platform_cfgs)
        if len(week_cats) < 3:
            eligible.sort(key=lambda pk: (
                0 if _platform_category(platform_cfgs.get(pk, {}).get('type', '')) not in week_cats else 1
            ))
    except Exception as e:
        log.warning('Rule 11 week diversity check failed: %s', e)

    return eligible


def pick_start_delay_secs() -> int:
    """Rule 13: random delay 0-50400s (14 hours) after 06:00 trigger."""
    return random.randint(0, 14 * 3600)


def pick_spread_delays(n_actions: int) -> list:
    """Rule 14: inter-action delays spreading n_actions over 2 hours. Returns n-1 delays."""
    if n_actions <= 1:
        return []
    window = 7200
    points = sorted(random.randint(60, window) for _ in range(n_actions - 1))
    if sum(points) > window:
        factor = window / sum(points)
        points = [max(1, int(p * factor)) for p in points]
    return points


def validate_plan_entry(
    eth_balance: float,
    health_results: list,
) -> tuple:
    """
    THE RULE — gate for plan_day().
    Must pass before any planning is allowed.

    Rules checked:
      Rule 5  — ETH balance guard (can we afford gas today?)
      Rule 6  — no emergency stop (borrow health ok)
    """
    if not balance_guard(eth_balance):
        return False, f'Rule5: ETH {eth_balance:.4f} < {get_eth_min()} — no plan today'
    if emergency_stop(health_results):
        return False, 'Rule6: emergency stop — borrow health critical, no plan today'
    return True, 'ok'


def validate_maintenance_entry(
    eth_balance: float,
    health_results: list,
) -> tuple:
    """
    THE RULE — gate for maintenance_job().
    Maintenance (closes + health) always runs regardless of balance,
    but blocks on catastrophic conditions only.

    Rules checked:
      Rule 6 — emergency stop check (log only, maintenance still runs closes)
    Note: balance guard does NOT block maintenance — we need to close positions
    even if ETH is low.
    """
    if emergency_stop(health_results):
        # Log but do NOT block — maintenance must still close positions
        return True, 'warning:Rule6 emergency stop active — skipping new opens only'
    return True, 'ok'


def validate_close_entry(
    pos_id: int,
    platform_key: str,
    platform_cfgs: dict,
) -> tuple:
    """
    THE RULE — gate for closing a position.
    Checks that the position/platform is known before executing close.

    Rules checked:
      Rule 1  — platform must exist in config (known platform)
      Rule 4  — position must actually be active in state.db
    """
    if platform_key not in platform_cfgs:
        return False, f'Rule1: unknown platform {platform_key!r} — cannot close'
    active_keys = {p[1] for p in _state.get_active()}
    if pos_id is not None:
        active_ids = {p[0] for p in _state.get_active()}
        if pos_id not in active_ids:
            return False, f'Rule4: position #{pos_id} not active in state.db'
    return True, 'ok'


def pre_action_validate(
    platform_key: str,
    platform_cfgs: dict,
    active_positions: list,
    health_results: list,
    today_opened_protocols: set,
    eth_balance: float,
) -> tuple:
    """
    THE RULE — pre-execution gate. Re-checks all rules at execution time.
    Called just before _open_platform() runs.

    Returns (ok: bool, reason: str).
    ok=True  → proceed.
    ok=False → reject; caller must repick and retry.

    Rules checked (in order):
      Rule 5  — ETH balance guard
      Rule 6  — emergency stop (health)
      Rule 7  — platform not already active
      Rule 8  — protocol not already opened today
      Rule 9  — not in cooldown
      Rule 10 — category cap not exceeded
    """
    # Rule 5: ETH balance
    if not balance_guard(eth_balance):
        return False, f'Rule5: ETH {eth_balance:.4f} < {get_eth_min()}'

    # Rule 6: emergency stop
    if emergency_stop(health_results):
        return False, 'Rule6: emergency stop (borrow health critical)'

    p     = platform_cfgs.get(platform_key, {})
    ptype = p.get('type', '')
    cat   = _platform_category(ptype)

    # Rule 7: not already active
    active_set = {pos[1] for pos in active_positions}
    if platform_key in active_set:
        return False, f'Rule7: {platform_key} already active'

    # Rule 8: protocol not opened today
    proto = get_protocol(platform_key, p)
    if proto in today_opened_protocols:
        return False, f'Rule8: protocol {proto!r} already opened today'

    # Rule 9: cooldown
    if is_in_cooldown(platform_key):
        return False, f'Rule9: {platform_key} in cooldown'

    # Rule 10: category cap
    counts = count_active_by_category(active_positions, platform_cfgs)
    mc  = get_max_concurrent()
    cap = mc.get(cat, 999)
    if counts.get(cat, 0) >= cap:
        return False, f'Rule10: {cat} cap {cap} reached ({counts.get(cat,0)} active)'

    # Rule 12a: supply/borrow collision (AAVE + Moonwell)
    _R12_SUPPLY = {'aave_supply', 'ctoken'}
    _R12_BORROW = {'aave_borrow', 'mw_borrow'}
    if ptype in _R12_SUPPLY:
        tok = p.get('token', '')
        for pos in active_positions:
            acfg = platform_cfgs.get(pos[1], {})
            if acfg.get('type') in _R12_BORROW and acfg.get('collateral_token') == tok:
                return False, f'Rule12a: {ptype} {tok} blocked — active borrow uses it as collateral'
    elif ptype in _R12_BORROW:
        col = p.get('collateral_token', '')
        for pos in active_positions:
            acfg = platform_cfgs.get(pos[1], {})
            if acfg.get('type') in _R12_SUPPLY and acfg.get('token') == col:
                return False, f'Rule12a: {ptype} collateral {col} blocked — active supply of same token'

    return True, 'ok'


def usdc_excess(usdc_balance_wei: int) -> int:
    """Rule 24/26: USDC wei above retention threshold (read from settings at call-time)."""
    try:
        import settings as _s
        retain_usd = float(_s.load().get('usdc_retain_usd', USDC_RETAIN_USD))
        retain_wei = int(retain_usd * 1e6)
    except Exception:
        retain_wei = USDC_RETAIN_WEI
    return max(usdc_balance_wei - retain_wei, 0)


def weth_excess(weth_balance_wei: int) -> int:
    """Rule 24/26: WETH wei above retention threshold (read from settings at call-time)."""
    try:
        import settings as _s
        retain_eth = float(_s.load().get('weth_retain_eth', WETH_RETAIN_ETH))
        retain_wei = int(retain_eth * 1e18)
    except Exception:
        retain_wei = WETH_RETAIN_WEI
    return max(weth_balance_wei - retain_wei, 0)
