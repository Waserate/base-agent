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
from dotenv import load_dotenv

load_dotenv()

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

# Phase 2a: run Sonnet diagnosis on new incidents. Diagnose-only (read-only) —
# no funds touched. At most DIAGNOSE_PER_SCAN per poll to bound quota usage.
REMEDIATION_ENABLED = os.getenv('REMEDIATION_ENABLED', '1').lower() not in ('0', 'false', 'no')
DIAGNOSE_PER_SCAN   = int(os.getenv('DIAGNOSE_PER_SCAN', '1'))

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


def _pos_still_active(wid: str, pos_id) -> bool:
    """Return True only if the position row is still status='active' in the wallet's DB.
    Prevents manual_withdraw entries for already-closed positions from looping forever."""
    if pos_id is None:
        return True  # can't verify → assume active (conservative)
    try:
        import wallet_manager as _wm, sqlite3 as _sq
        w = _wm.get_wallet(wid)
        db_fn = (w or {}).get('state_db', f'state_{wid}.db')
        db_path = os.path.join(_DIR, db_fn)
        if not os.path.exists(db_path):
            return True
        conn = _sq.connect(db_path)
        row = conn.execute(
            "SELECT status FROM positions WHERE id=?", (pos_id,)
        ).fetchone()
        conn.close()
        return (row is None) or (row[0] == 'active')
    except Exception:
        return True  # can't verify → assume active


def _scan_manual_withdraw():
    """manual_withdraw_<wid>.json -> one incident per stuck position (7-retry exhausted).
    Skips entries where DB row is already closed — prevents infinite rediagnose loops."""
    for path in glob.glob(os.path.join(_CACHE, 'manual_withdraw_*.json')):
        wid     = _wid_from(path, 'manual_withdraw_')
        entries = _load_json(path, [])
        seen    = set()
        for e in entries:
            pid = e.get('pos_id')
            if pid in seen:
                continue
            seen.add(pid)
            if not _pos_still_active(wid, pid):
                log.debug(f'[manual-withdraw] skip pos#{pid} {e.get("platform","")} — already closed in DB')
                continue
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


def _run_diagnoses():
    """Phase 2a+2b — diagnose freshly detected incidents; run live remediation if enabled."""
    if not REMEDIATION_ENABLED:
        return
    pending = [i for i in store.get_all()['incidents'] if i['status'] == 'detected']
    if not pending:
        return
    try:
        import remediation_agent as ra
    except Exception as e:
        log.warning(f'remediation agent unavailable ({e}) — staying Phase 1 (detect only)')
        return
    for inc in pending[:DIAGNOSE_PER_SCAN]:
        log.info(f'diagnosing {inc["id"]} ({inc["signal"]}/{inc["platform"]}) ...')
        try:
            diag = ra.diagnose(inc['id'])
            cat  = diag.get('category', '?')
            log.info(f'  -> {cat}: {diag.get("root_cause","")[:90]}')

            # Phase 2b: run live remediation for auto-fixable state_drift incidents
            if (ra.MODE == 'live'
                    and cat == 'state_drift'
                    and diag.get('auto_fixable')
                    and diag.get('confidence', 'low') in ('medium', 'high')):
                log.info(f'  [2b] auto-fixable state_drift — remediating ...')
                try:
                    result = ra.remediate(inc['id'])
                    log.info(f'  [2b] remediate -> {result.get("status")}')
                except Exception as e2:
                    log.error(f'  [2b] remediate crashed: {e2}')
            # Phase 3: code_bug incidents → code_fix_agent (queued via remediate)
            elif ra.MODE == 'live' and cat == 'code_bug':
                log.info(f'  [3] code_bug — queuing code-fix agent ...')
                try:
                    ra.remediate(inc['id'])   # dispatches code_fix_agent internally
                except Exception as e2:
                    log.error(f'  [3] code_fix dispatch crashed: {e2}')
            elif cat in ('external', 'unknown'):
                # Check if bot already self-resolved — no user action needed
                root = (diag.get('root_cause') or '').lower()
                self_resolved = any(k in root for k in (
                    'self-resolved', 'self resolved', 'no immediate action',
                    'already resolved', 'recovery succeeded', 'repicked',
                ))
                if self_resolved:
                    store.update(inc['id'], status='resolved')
                    log.info(f'  -> auto-resolved (external but self-resolved by bot)')
                else:
                    store.update(inc['id'], status='needs_manual')
                    log.info(f'  -> marked needs_manual (category={cat}, waiting on human)')
        except Exception as e:
            log.error(f'  diagnosis crashed: {e}')


def _run_remediations():
    """Phase 2b sweep — remediate already-diagnosed auto_fixable incidents (e.g. after watcher restart)."""
    if not REMEDIATION_ENABLED:
        return
    diagnosed = [i for i in store.get_all()['incidents']
                 if i['status'] == 'diagnosed'
                 and i.get('auto_fixable')
                 and i.get('confidence', 'low') in ('medium', 'high')]
    if not diagnosed:
        return
    try:
        import remediation_agent as ra
    except Exception as e:
        log.warning(f'remediation agent unavailable ({e})')
        return
    if ra.MODE != 'live':
        return
    for inc in diagnosed[:DIAGNOSE_PER_SCAN]:
        cat = inc.get('category', '')
        if cat not in ('state_drift', 'code_bug'):
            continue
        log.info(f'  [sweep] {inc["id"]} ({cat}/{inc.get("platform","")}) — remediating ...')
        try:
            result = ra.remediate(inc['id'])
            log.info(f'  [sweep] -> {result.get("status")}')
        except Exception as e2:
            log.error(f'  [sweep] crashed: {e2}')


def _scan_positions_onchain():
    """Proactive ghost-detection: scan every active DB position for zero on-chain balance.
    Creates an incident immediately — no need to wait for bot withdrawal failure.
    Runs on every poll cycle; incident_store deduplication prevents spam."""
    try:
        import remediation_agent as ra
        import wallet_manager as _wm
        import sqlite3 as _sq
    except Exception as e:
        log.warning(f'[onchain-scan] import failed: {e}')
        return

    wallets = _wm.load_wallets()
    if not wallets:
        return

    for w in wallets:
        wid   = w.get('id', 'default')
        db_fn = w.get('state_db', f'state_{wid}.db')
        db_path = os.path.join(_DIR, db_fn)
        if not os.path.exists(db_path):
            continue
        try:
            conn = _sq.connect(db_path)
            rows = conn.execute(
                "SELECT id, platform FROM positions WHERE status='active'"
            ).fetchall()
            conn.close()
        except Exception as e:
            log.warning(f'[onchain-scan] DB read failed {db_fn}: {e}')
            continue

        for pos_id, platform in rows:
            try:
                result = ra._onchain_balance(platform, wid)
            except Exception as e:
                log.debug(f'[onchain-scan] pos#{pos_id} {platform} check error: {e}')
                continue

            if 'GHOST POSITION' in result:
                log.info(f'[onchain-scan] ghost detected pos#{pos_id} {platform} wallet={wid}')
                store.record(
                    'withdraw_failed', wallet=wid, platform=platform,
                    pos_id=pos_id, severity='warn',
                    title=f'{platform}#{pos_id} ghost — balance=0 on-chain',
                    detail=f'on-chain balance = 0 but DB status=active. {result}',
                )


_CLEANUP_DAYS = int(os.getenv('POSITION_CLEANUP_DAYS', '7'))  # delete closed rows older than N days

def _cleanup_closed_positions():
    """Two-pass cleanup every poll cycle:
    Pass 1 — cross-DB dedup: if platform+entry_date is closed in ANY DB, close it in ALL DBs.
             Prevents ghost-retry when same position drifted into multiple DBs.
    Pass 2 — age purge: delete closed rows with expiry_date older than POSITION_CLEANUP_DAYS."""
    try:
        import sqlite3 as _sq, wallet_manager as _wm
        from datetime import timedelta

        dbs = []
        for w in _wm.load_wallets():
            db = w.get('state_db')
            if db:
                p = os.path.join(_DIR, db)
                if p not in dbs:
                    dbs.append(p)
        dbs = [p for p in dbs if os.path.exists(p)]

        # Pass 1: collect all (platform, entry_date) keys that are closed in any DB
        closed_keys = set()
        for db_path in dbs:
            conn = _sq.connect(db_path)
            rows = conn.execute(
                "SELECT platform, entry_date FROM positions WHERE status='closed'"
            ).fetchall()
            conn.close()
            closed_keys.update(rows)

        # Close matching active rows in every other DB
        for db_path in dbs:
            conn = _sq.connect(db_path)
            synced = 0
            for platform, entry_date in closed_keys:
                cur = conn.execute(
                    "UPDATE positions SET status='closed' WHERE platform=? AND entry_date=? AND status='active'",
                    (platform, entry_date)
                )
                synced += cur.rowcount
            conn.commit()
            conn.close()
            if synced:
                log.info(f'[cleanup] {os.path.basename(db_path)}: cross-DB synced {synced} active→closed')

        # Pass 2: delete old closed rows
        cutoff = (date.today() - timedelta(days=_CLEANUP_DAYS)).isoformat()
        for db_path in dbs:
            conn = _sq.connect(db_path)
            cur = conn.execute(
                "DELETE FROM positions WHERE status='closed' AND expiry_date <= ?",
                (cutoff,)
            )
            deleted = cur.rowcount
            conn.commit()
            conn.close()
            if deleted:
                log.info(f'[cleanup] {os.path.basename(db_path)}: purged {deleted} old closed rows (expiry <= {cutoff})')
    except Exception as e:
        log.warning(f'[cleanup] failed: {e}')


def scan_once():
    store.set_agent_state('watching')
    cursors = _cursors()
    try:
        _scan_maintenance()
        _scan_manual_withdraw()
        _scan_action_log(cursors)
        _scan_positions_onchain()
        _run_diagnoses()
        _run_remediations()
        _cleanup_closed_positions()
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
