"""
daily_briefing.py — Morning task card for the day.

Generates a structured briefing showing:
  - Wallet state
  - Active positions
  - Today's scheduled trigger + action count estimate
  - Warnings (expiry, health, etc.)

Usage:
    python daily_briefing.py          # print card
    from daily_briefing import build  # returns dict (for API)
"""

import os, json, logging, random
from datetime import date, datetime, timedelta, time as dtime

from dotenv import load_dotenv
load_dotenv()

import state
import executor
from name_utils import _auto_name

log = logging.getLogger(__name__)

with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as _f:
    CFG = json.load(_f)

BRIEFING_BKK_HOUR = 7    # card generated 07:00 BKK
ACTION_MIN_BKK    = 7*60+1   # 07:01 BKK earliest action
ACTION_MAX_BKK    = 23*60+50 # 23:50 BKK latest action
_CACHE_DIR = os.path.join(os.path.dirname(__file__), 'cache')

def get_plan_file() -> str:
    wid = os.environ.get('WALLET_ID', 'default')
    return os.path.join(_CACHE_DIR, f'plan_{wid}.json')

def _get_rule_log_file() -> str:
    wid = os.environ.get('WALLET_ID', 'default')
    return os.path.join(_CACHE_DIR, f'rule_log_{wid}.json')


def _get_wallet_state() -> dict:
    try:
        eth      = executor.get_eth_balance()
        eth_usd  = executor.get_eth_usd_price()
        usdc_addr = CFG['tokens']['USDC']['address']
        usdc     = executor.get_token_balance(usdc_addr, decimals=6)
        return {
            'eth':       round(eth, 4),
            'eth_usd':   round(eth_usd, 0),
            'usdc':      round(usdc, 2),
            'total_usd': round(eth * eth_usd + usdc, 2),
            'ok':        True,
        }
    except Exception as e:
        return {'ok': False, 'error': str(e), 'eth': 0, 'total_usd': 0}


def _count_candidates(active_set: set) -> int:
    try:
        import rule_engine
        all_p = [k for k, v in CFG['platforms'].items()
                 if isinstance(v, dict) and v.get('type') not in ('aero_vote',)]
        active_pos = state.get_active()
        cands = rule_engine.filter_candidates(all_p, active_set, set(), CFG['platforms'], active_pos, [])
        return len(cands)
    except Exception:
        return 0


def _estimate_actions(candidates: int) -> int:
    try:
        import rule_engine
        return rule_engine.pick_action_count()
    except Exception:
        return 2


def _warnings(active: list) -> list:
    warns = []
    today = date.today()
    for pos in active:
        platform = pos[1]
        expiry   = date.fromisoformat(pos[5])
        days_left = (expiry - today).days
        if days_left <= 3:
            warns.append(f'Position #{pos[0]} {platform} expires in {days_left}d')

    # veAERO special check
    for pos in active:
        if pos[1] == 'aero_vote':
            try:
                from aero_vote import VE_ADDR, VE_ABI
                token_id = int(str(pos[3]).split('|')[0])
                ve = executor.w3.eth.contract(
                    address=executor.Web3.to_checksum_address(VE_ADDR), abi=VE_ABI
                )
                locked = ve.functions.locked(token_id).call()
                lock_end = locked[1]
                now_ts = executor.w3.eth.get_block('latest')['timestamp']
                days_left = (lock_end - now_ts) // 86400
                if days_left <= 14:
                    warns.append(f'veAERO tokenId={token_id} lock expires in {days_left}d')
            except Exception:
                pass

    return warns


def _rule_log_plan(ok: bool, reason: str):
    """Write plan-context rule event to rule_log.json."""
    from datetime import datetime as _dt
    entry = {
        'ts':      _dt.now().strftime('%H:%M:%S'),
        'date':    date.today().isoformat(),
        'context': 'plan',
        'original': 'plan_day',
        'current':  'plan_day',
        'attempt':  1,
        'ok':       ok,
        'reason':   reason,
        'outcome':  'allowed' if ok else 'blocked',
    }
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        log_file = _get_rule_log_file()
        try:
            with open(log_file) as f:
                entries = json.load(f)
        except Exception:
            entries = []
        entries.append(entry)
        entries = entries[-100:]
        with open(log_file, 'w') as f:
            json.dump(entries, f, indent=2)
    except Exception as e:
        log.warning(f'_rule_log_plan write failed: {e}')


def plan_day() -> list:
    """
    Pick 1-3 platforms + random BKK times (07:01-23:50).
    Returns list of action dicts, sorted by time.
    Also saves to cache/daily_plan.json.
    """
    state.init_db()
    active     = state.get_active()
    active_set = {p[1] for p in active}
    today      = date.today()

    try:
        import rule_engine
        import executor as _exec

        eth = _exec.get_eth_balance()

        # THE RULE — plan entry gate
        ok, reason = rule_engine.validate_plan_entry(eth, [])
        _rule_log_plan(ok, reason)
        if not ok:
            log.warning(f'THE RULE [plan]: BLOCKED — {reason}')
            return []
        log.info(f'THE RULE [plan]: allowed — {reason}')

        all_p = [k for k, v in CFG['platforms'].items()
                 if isinstance(v, dict) and v.get('type') not in ('aero_vote',)]

        # Rules 6-11: filter candidates
        candidates = rule_engine.filter_candidates(
            all_p, active_set, set(), CFG['platforms'], active, []
        )
        random.shuffle(candidates)

        # Rule 3: pick action count with weights (40%:1, 40%:2, 20%:3)
        n = rule_engine.pick_action_count()

        # Rule 8: deduplicate by protocol during selection
        today_protocols: set = set()
        picks = []
        for pk in candidates:
            if len(picks) >= n:
                break
            proto = rule_engine.get_protocol(pk, CFG['platforms'].get(pk, {}))
            if proto not in today_protocols:
                today_protocols.add(proto)
                picks.append(pk)

    except Exception as e:
        log.warning(f'plan_day candidate selection failed: {e}')
        picks = []

    # Pick n distinct random minutes in [07:01, 23:50], sorted ascending
    all_minutes = list(range(ACTION_MIN_BKK, ACTION_MAX_BKK + 1))
    time_mins = sorted(random.sample(all_minutes, min(n, len(all_minutes))))

    actions = []
    for i, (platform, mins) in enumerate(zip(picks, time_mins)):
        h, m = divmod(mins, 60)
        p_cfg = CFG['platforms'].get(platform, {})
        ptype = p_cfg.get('type', '')
        token = p_cfg.get('token') or p_cfg.get('borrow_token', '')

        # Classify display type
        if 'borrow' in ptype:
            disp_type = 'BORROW'
        elif 'lp' in ptype:
            disp_type = 'LP'
        elif ptype == 'aero_vote':
            disp_type = 'VOTE'
        else:
            disp_type = 'LEND'

        # BKK datetime → UTC datetime string
        bkk_dt = datetime.combine(today, dtime(h, m))
        utc_dt = bkk_dt - timedelta(hours=7)

        # Rule 12: random tiered USD amount ($5-15)
        try:
            import rule_engine as _re
            usd_est = _re.pick_amount_usd()
        except Exception:
            usd_est = 5.0

        try:
            import settings as _settings
            expiry_days = _settings.expiry_for_type(ptype)
        except Exception:
            days_cfg    = p_cfg.get('expiry_days', [3, 5])
            expiry_days = random.randint(int(days_cfg[0]), int(days_cfg[1]))

        try:
            import rule_engine as _re
            protocol = _re.get_protocol(platform, p_cfg)
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

    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(get_plan_file(), 'w') as f:
        json.dump({'date': today.isoformat(), 'actions': actions}, f, indent=2)

    log.info(f'plan_day: {n} actions planned for {today}')
    return actions


def load_plan() -> list:
    """Load today's plan from cache. Returns [] if missing or stale."""
    try:
        with open(get_plan_file()) as f:
            data = json.load(f)
        if data.get('date') != date.today().isoformat():
            return []
        return data.get('actions', [])
    except Exception:
        return []


def build() -> dict:
    """Build briefing dict. Called by API and print_card."""
    state.init_db()
    today     = date.today()
    active    = state.get_active()
    active_set = {p[1] for p in active}

    wallet    = _get_wallet_state()
    n_cands   = _count_candidates(active_set)
    n_actions = _estimate_actions(n_cands)
    warnings  = _warnings(active)

    # Load today's plan (if exists)
    plan = load_plan()

    # Schedule: fixed events
    _maint_flag = os.path.join(os.path.dirname(__file__), 'cache', f'maintenance_done_{today.isoformat()}.flag')
    _maint_done = os.path.exists(_maint_flag)
    schedule = [
        {'time': '07:00 BKK', 'event': 'Briefing + plan day', 'done': True},
        {'time': '07:05 BKK', 'event': 'Maintenance (health + closes)', 'done': _maint_done},
    ]
    # Add each planned action as a schedule item
    for a in plan:
        tick = a.get('done', False)
        schedule.append({'time': f'{a["time_bkk"]} BKK', 'event': _auto_name(a['platform']), 'done': tick})
    if today.weekday() == 0:
        schedule.append({'time': 'After 07:05', 'event': 'Weekly report generated', 'done': False})

    wid = os.environ.get('WALLET_ID', '')
    try:
        import wallet_manager as _wm_b
        _we = _wm_b.get_wallet(wid)
        wname = _we.get('name', wid) if _we else wid
    except Exception:
        wname = wid

    return {
        'date':         today.isoformat(),
        'weekday':      today.strftime('%A'),
        'wallet_id':    wid,
        'wallet_name':  wname,
        'wallet':       wallet,
        'active_count': len(active),
        'active':       [{'id': p[0], 'platform': p[1], 'token': p[2],
                          'expiry': p[5], 'days_left': (date.fromisoformat(p[5]) - today).days}
                         for p in active],
        'candidates':   n_cands,
        'n_actions':    len(plan) if plan else n_actions,
        'plan':         plan,
        'schedule':     schedule,
        'warnings':     warnings,
        'generated_at': datetime.now().strftime('%H:%M:%S'),
    }


def print_card(data: dict | None = None):
    """Print formatted ASCII card to stdout/log."""
    if data is None:
        data = build()

    w = data['wallet']
    eth_str  = f"{w['eth']:.4f} ETH (~${w['total_usd']:.0f})" if w.get('ok') else 'RPC error'
    W = 52

    def line(txt='', fill=' '):
        txt = str(txt)
        pad = W - 2 - len(txt)
        return f'  {txt}{fill * max(0, pad)}'

    SEP  = '  ' + '=' * (W + 2)
    TOP  = SEP
    BOT  = SEP

    def row(txt): return '  | ' + f'{txt:<{W-1}}' + '|'
    def div():    return '  |' + '-' * W + '|'

    lines = [
        TOP,
        row(f'DAILY BRIEFING  {data["date"]}  ({data["weekday"].upper()[:3]})  [{data.get("wallet_name", "")}]'),
        div(),
        row(f'Wallet:   {eth_str}'),
        row(f'USDC:     ${w.get("usdc", 0):.2f}'),
        row(f'Active:   {data["active_count"]} position(s)'),
        div(),
        row('SCHEDULE'),
    ]
    for s in data['schedule']:
        tick = '[x]' if s['done'] else '[ ]'
        lines.append(row(f'  {tick} {s["time"]:18} {s["event"]}'))

    lines += [
        div(),
        row(f'PLANNED ACTIONS: {data["n_actions"]}  (from {data["candidates"]} eligible)'),
    ]

    plan = data.get('plan', [])
    if plan:
        lines.append(div())
        lines.append(row('  TIME      TYPE    TOKEN    PLATFORM              USD'))
        for a in plan:
            tick = '[x]' if a.get('done') else '[ ]'
            line_txt = f'  {tick} {a["time_bkk"]}  {a["disp_type"]:<7} {a["token"]:<8} {a["display_name"][:20]:<20} ${a["usd_est"]:.2f}'
            lines.append(row(line_txt))

    if data['warnings']:
        lines.append(div())
        lines.append(row('WARNINGS'))
        for w_txt in data['warnings']:
            lines.append(row(f'  !! {w_txt}'))

    lines.append(BOT)

    card = '\n'.join(lines)
    print('\n' + card + '\n')
    log.info('\n' + card)
    return card


def print_inactive_card(w_entry: dict):
    """Print minimal card for inactive wallet — address + ETH balance, no TX capability."""
    wid  = w_entry.get('id', '?')
    addr = w_entry.get('address', '?')
    has_pk = bool(w_entry.get('private_key', ''))
    W = 52

    def row(txt): return '  | ' + f'{txt:<{W-1}}' + '|'
    def div():    return '  |' + '-' * W + '|'
    SEP = '  ' + '=' * (W + 2)

    eth_str = 'RPC error'
    try:
        from web3 import Web3
        bal = executor.w3_read.eth.get_balance(Web3.to_checksum_address(addr))
        eth_usd = executor.get_eth_usd_price()
        eth = bal / 1e18
        eth_str = f'{eth:.4f} ETH (~${eth * eth_usd:.0f})'
    except Exception:
        pass

    pk_status = 'PK=SET  active=false' if has_pk else 'PK=---  active=false'
    lines = [
        SEP,
        row(f'WALLET: {wid.upper()}  (INACTIVE)'),
        div(),
        row(f'Address:  {addr}'),
        row(f'ETH:      {eth_str}'),
        row(f'Status:   {pk_status}'),
        SEP,
    ]
    card = '\n'.join(lines)
    print('\n' + card + '\n')
    log.info('\n' + card)


if __name__ == '__main__':
    import logging as _l
    _l.basicConfig(level=_l.INFO, format='%(message)s',
                   handlers=[_l.StreamHandler()])
    print_card()
