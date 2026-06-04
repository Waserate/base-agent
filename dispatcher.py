"""
dispatcher.py — Centralized multi-wallet action planner.

Generates non-overlapping daily plans for all active wallets.
Called by briefing_and_plan() in agent.py instead of daily_briefing.plan_day().

Output files:
  cache/plan_{wallet_id}.json  — per-wallet plan (consumed by agent scheduler)
  cache/plan_all.json          — combined view for ALL dashboard tab
"""

import os, json, random, logging, importlib, sys
from datetime import date, datetime, timedelta, time as dtime

log = logging.getLogger(__name__)

_CACHE_DIR = os.path.join(os.path.dirname(__file__), 'cache')

with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as _f:
    CFG = json.load(_f)

ACTION_MIN_BKK = 7*60 + 1    # 07:01 BKK
ACTION_MAX_BKK = 23*60 + 50  # 23:50 BKK


def plan_all_wallets() -> dict:
    """
    Centralized planner for all active wallets with private keys.
    Returns {wallet_id: [actions]}.
    Saves cache/plan_{wid}.json per wallet + cache/plan_all.json combined.
    Always restores original wallet context when done.
    """
    import wallet_manager as _wm
    import rule_engine
    import settings as _settings
    from name_utils import _auto_name

    original_wid = os.environ.get('WALLET_ID', 'default')
    all_wallets  = _wm.load_wallets()
    active_wallets = [w for w in all_wallets
                      if w.get('active', True) and w.get('private_key', '')]

    if not active_wallets:
        log.warning('dispatcher: no active wallets with private keys')
        return {}

    today        = date.today()
    all_platforms = [k for k, v in CFG['platforms'].items()
                     if isinstance(v, dict) and v.get('type') not in ('aero_vote',)]

    globally_assigned: set = set()   # platforms already picked by any wallet
    results: dict = {}

    for w in active_wallets:
        wid = w['id']
        log.info(f'dispatcher: planning {wid}...')

        _wm.switch_context(wid)               # sets WALLET_ID env + reloads executor/state
        for mod in ('state', 'executor'):
            if mod in sys.modules:
                importlib.reload(sys.modules[mod])

        import state, executor
        state.init_db()

        try:
            eth = executor.get_eth_balance()
            ok, reason = rule_engine.validate_plan_entry(eth, [])
            if not ok:
                log.warning(f'dispatcher: {wid} BLOCKED — {reason}')
                results[wid] = []
                _save_wallet_plan(wid, [])
                continue

            active = state.get_active()
            active_set = {p[1] for p in active}

            # Per-wallet rule filter, then subtract globally assigned
            candidates = rule_engine.filter_candidates(
                all_platforms, active_set, set(), CFG['platforms'], active, []
            )
            candidates = [c for c in candidates if c not in globally_assigned]
            random.shuffle(candidates)

            n = rule_engine.pick_action_count()

            wallet_protocols: set = set()
            picks = []
            for pk in candidates:
                if len(picks) >= n:
                    break
                proto = rule_engine.get_protocol(pk, CFG['platforms'].get(pk, {}))
                if proto not in wallet_protocols:
                    wallet_protocols.add(proto)
                    picks.append(pk)
                    globally_assigned.add(pk)

            actions = _build_actions(picks, today, _auto_name, rule_engine, _settings)
            results[wid] = actions
            _save_wallet_plan(wid, actions)
            log.info(f'dispatcher: {wid} — {len(actions)} actions: '
                     f'{[a["platform"] for a in actions]}')

        except Exception as e:
            log.error(f'dispatcher: {wid} planning failed: {e}')
            results[wid] = []
            _save_wallet_plan(wid, [])

    _save_all_plan(results, today)

    # Restore original wallet context
    try:
        _wm.switch_context(original_wid)
        for mod in ('state', 'executor'):
            if mod in sys.modules:
                importlib.reload(sys.modules[mod])
    except Exception as e:
        log.warning(f'dispatcher: failed to restore context {original_wid!r}: {e}')

    return results


def _build_actions(picks: list, today: date,
                   _auto_name, rule_engine, _settings) -> list:
    n = len(picks)
    if n == 0:
        return []

    all_minutes = list(range(ACTION_MIN_BKK, ACTION_MAX_BKK + 1))
    time_mins   = sorted(random.sample(all_minutes, min(n, len(all_minutes))))

    actions = []
    for i, (platform, mins) in enumerate(zip(picks, time_mins)):
        h, m  = divmod(mins, 60)
        p_cfg = CFG['platforms'].get(platform, {})
        ptype = p_cfg.get('type', '')
        token = p_cfg.get('token') or p_cfg.get('borrow_token', '')

        if 'borrow' in ptype:      disp_type = 'BORROW'
        elif 'lp' in ptype:        disp_type = 'LP'
        elif ptype == 'aero_vote': disp_type = 'VOTE'
        else:                      disp_type = 'LEND'

        bkk_dt = datetime.combine(today, dtime(h, m))
        utc_dt = bkk_dt - timedelta(hours=7)

        try:
            usd_est = rule_engine.pick_amount_usd()
        except Exception:
            usd_est = 5.0

        try:
            expiry_days = _settings.expiry_for_type(ptype)
        except Exception:
            days_cfg = p_cfg.get('expiry_days', [3, 5])
            expiry_days = random.randint(int(days_cfg[0]), int(days_cfg[1]))

        try:
            protocol = rule_engine.get_protocol(platform, p_cfg)
        except Exception:
            protocol = platform.split('_')[0]

        actions.append({
            'idx':          i + 1,
            'platform':     platform,
            'display_name': _auto_name(platform),
            'protocol':     protocol,
            'type':         ptype,
            'disp_type':    disp_type,
            'token':        token,
            'usd_est':      usd_est,
            'expiry_days':  expiry_days,
            'time_bkk':     f'{h:02d}:{m:02d}',
            'run_at_utc':   utc_dt.isoformat(),
            'date':         today.isoformat(),
            'done':         False,
        })
    return actions


def _save_wallet_plan(wallet_id: str, actions: list):
    os.makedirs(_CACHE_DIR, exist_ok=True)
    path = os.path.join(_CACHE_DIR, f'plan_{wallet_id}.json')
    with open(path, 'w') as f:
        json.dump({'date': date.today().isoformat(), 'actions': actions}, f, indent=2)


def _save_all_plan(results: dict, today: date):
    """Combine all wallet plans into plan_all.json for the ALL dashboard tab."""
    import wallet_manager as _wm
    name_map = {w['id']: w.get('name', w['id']) for w in _wm.load_wallets()}

    combined = []
    for wid, actions in results.items():
        for a in actions:
            entry = dict(a)
            entry['wallet_id']   = wid
            entry['wallet_name'] = name_map.get(wid, wid)
            combined.append(entry)
    combined.sort(key=lambda x: x['time_bkk'])

    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(os.path.join(_CACHE_DIR, 'plan_all.json'), 'w') as f:
        json.dump({'date': today.isoformat(), 'actions': combined}, f, indent=2)
