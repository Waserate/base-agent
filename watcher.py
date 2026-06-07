"""
watcher.py — Incident detector for the Base agent (Phase 1: detect + display).

Separate process. Polls the signal files the bot already writes and records
incidents into incident_store (cache/incidents.json), which the dashboard shows.

Phase 1 does NOT call Sonnet or run any remediation — it only surfaces what
went wrong so you can see the panel working. Phase 2 wires the SDK agent.

Signals watched:
  cache/maintenance_done_<date>.json   status=partial -> per failure
  cache/manual_withdraw_<wid>.json     -> per stuck position  (withdraw failed 7x)
  cache/action_log_<wid>.json          step in fail/repick/recovery -> per entry

Run:
  python watcher.py            # loop (default 300s, env WATCHER_POLL_S)
  python watcher.py --once     # single scan (for testing)
"""

import os, sys, json, time, glob, logging
from datetime import date

import incident_store as store

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [watcher] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

_DIR       = os.path.dirname(__file__)
_CACHE     = os.path.join(_DIR, 'cache')
_CURSOR    = os.path.join(_CACHE, 'watcher_cursor.json')
POLL_S     = int(os.getenv('WATCHER_POLL_S', '300'))

# action_log step -> (signal, severity). Steps we treat as incidents.
_STEP_SIGNALS = {
    'fail':     ('action_fail', 'warn'),
    'repick':   ('repick',      'warn'),
    'recovery': ('recovery',    'warn'),
}


def _load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _cursors() -> dict:
    return _load_json(_CURSOR, {})


def _save_cursors(c: dict):
    os.makedirs(_CACHE, exist_ok=True)
    with open(_CURSOR, 'w', encoding='utf-8') as f:
        json.dump(c, f)


def _wid_from(path: str, prefix: str) -> str:
    base = os.path.basename(path)
    return base[len(prefix):-len('.json')] or 'default'


# ── Detectors ────────────────────────────────────────────────────────────────

def _scan_maintenance():
    """maintenance_done_<date>.json status=partial -> one incident per failure."""
    today = date.today().isoformat()
    path  = os.path.join(_CACHE, f'maintenance_done_{today}.json')
    data  = _load_json(path, None)
    if not data or data.get('status') != 'partial':
        return
    for fail in data.get('failures', []):
        store.record(
            'maintenance_partial', platform=str(fail), severity='warn',
            title=f'maintenance partial: {fail}',
            detail=f'maintenance_job finished partial — failure: {fail}',
        )


def _scan_manual_withdraw():
    """manual_withdraw_<wid>.json -> one incident per stuck position (7-retry exhausted)."""
    for path in glob.glob(os.path.join(_CACHE, 'manual_withdraw_*.json')):
        wid     = _wid_from(path, 'manual_withdraw_')
        entries = _load_json(path, [])
        seen    = set()
        for e in entries:
            pid = e.get('pos_id')
            if pid in seen:
                continue
            seen.add(pid)
            store.record(
                'withdraw_failed', wallet=wid, platform=e.get('platform', ''),
                pos_id=pid, severity='warn',
                title=f'{e.get("platform","?")}#{pid} withdraw stuck',
                detail=f'withdraw failed all retries (expired {e.get("expiry","?")}) — '
                       f'needs diagnosis. wallet={wid}',
            )


def _scan_action_log(cursors: dict):
    """action_log_<wid>.json new entries with fail/repick/recovery steps.
    Skips backlog on first run (cursor initialised to current length)."""
    for path in glob.glob(os.path.join(_CACHE, 'action_log_*.json')):
        wid     = _wid_from(path, 'action_log_')
        entries = _load_json(path, [])
        ckey    = os.path.basename(path)
        start   = cursors.get(ckey)
        if start is None:               # first run — skip history, watch forward only
            cursors[ckey] = len(entries)
            continue
        for e in entries[start:]:
            step = e.get('step', '')
            sig  = _STEP_SIGNALS.get(step)
            if not sig:
                continue
            signal, severity = sig
            detail = e.get('detail', '')
            # systemic give-up — escalate
            if 'no valid replacement' in detail.lower() or 'no replacement' in detail.lower():
                signal, severity = 'systemic_giveup', 'critical'
            store.record(
                signal, wallet=wid, platform=e.get('platform', ''), severity=severity,
                title=f'{e.get("display_name") or e.get("platform","?")}: {step}',
                detail=detail or step,
            )
        cursors[ckey] = len(entries)


def scan_once():
    store.set_agent_state('watching')
    cursors = _cursors()
    try:
        _scan_maintenance()
        _scan_manual_withdraw()
        _scan_action_log(cursors)
    finally:
        _save_cursors(cursors)
        store.heartbeat()


def main():
    once = '--once' in sys.argv
    log.info(f'watcher start — poll={POLL_S}s  once={once}')
    if once:
        scan_once()
        log.info('single scan done')
        return
    while True:
        try:
            scan_once()
        except Exception as e:
            log.error(f'scan error: {e}')
        time.sleep(POLL_S)


if __name__ == '__main__':
    main()
