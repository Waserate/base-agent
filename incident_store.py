"""
incident_store.py — Shared incident store for the remediation watcher + dashboard.

Single JSON file (cache/incidents.json) that the watcher writes and the
dashboard reads. Phase 1: detection + display only (no Sonnet, no remediation).

Schema:
{
  "agent_state": "idle|watching|working|waiting",
  "updated":     "<iso ts — heartbeat so dashboard knows the watcher is alive>",
  "incidents": [
    {
      "id":         "inc_<date>_<time>_<key-hash>",
      "key":        "<signal>:<wallet>:<platform>:<pos_id>",  # dedup key
      "first_seen": "<iso>",
      "last_seen":  "<iso>",
      "count":      <int>,            # times this key fired within cooldown
      "days_seen":  ["YYYY-MM-DD"],   # distinct days — recurrence signal (trigger #6)
      "wallet":     "<wid>",
      "platform":   "<platform_key>",
      "pos_id":     <int|null>,
      "signal":     "<signal name>",
      "severity":   "info|warn|critical",
      "title":      "<short>",
      "detail":     "<longer>",
      "status":     "detected|investigating|resolved|needs_approval|alert",
      "actions":    [],              # remediation actions taken (Phase 2)
      "proposal":   null             # code-fix proposal (Phase 3)
    }
  ]
}
"""

import os, json, hashlib
from datetime import datetime, date

_CACHE_DIR = os.path.join(os.path.dirname(__file__), 'cache')
_PATH      = os.path.join(_CACHE_DIR, 'incidents.json')

DEFAULT_COOLDOWN_S = 3600   # same key within 1h = same incident (bump, don't duplicate)
MAX_INCIDENTS      = 200    # keep the file bounded; prune oldest beyond this


def _now() -> str:
    return datetime.now().isoformat(timespec='seconds')


def _load() -> dict:
    try:
        with open(_PATH, encoding='utf-8') as f:
            data = json.load(f)
        data.setdefault('agent_state', 'idle')
        data.setdefault('incidents', [])
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {'agent_state': 'idle', 'updated': _now(), 'incidents': []}


def _save(data: dict) -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    data['updated'] = _now()
    tmp = _PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _PATH)   # atomic — dashboard never reads a half-written file


def _key(signal: str, wallet: str, platform: str, pos_id) -> str:
    return f'{signal}:{wallet}:{platform}:{pos_id if pos_id is not None else "-"}'


def record(signal: str, *, wallet: str = 'default', platform: str = '',
           title: str = '', detail: str = '', pos_id=None,
           severity: str = 'warn', cooldown: int = DEFAULT_COOLDOWN_S) -> dict:
    """
    Record (or bump) an incident. Dedup by (signal, wallet, platform, pos_id).

    - If an OPEN incident with the same key exists and was last seen within
      `cooldown` seconds: bump count + last_seen, track the day, return it.
    - Otherwise create a new incident (a recurrence after cooldown / after
      resolution becomes a fresh row, but inherits days_seen for trigger #6).
    Returns the incident dict.
    """
    data  = _load()
    key   = _key(signal, wallet, platform, pos_id)
    today = date.today().isoformat()
    now   = _now()

    # Scan newest-first so we find the most-recent non-resolved incident first.
    # Scanning oldest-first caused a bug: the loop broke on the first 'resolved' row
    # (which was always the oldest) and fell through to create a duplicate open incident
    # even when a later 'diagnosed'/'needs_manual' row existed for the same key.
    for inc in reversed(data['incidents']):
        if inc['key'] != key:
            continue
        if inc['status'] == 'resolved':
            continue  # skip resolved rows — keep scanning for a non-resolved one
        # Found the most-recent non-resolved incident for this key.
        # Update in-place; never create a duplicate open incident.
        inc['count']    += 1
        inc['last_seen'] = now
        if today not in inc['days_seen']:
            inc['days_seen'].append(today)
        if len(inc['days_seen']) >= 3 and inc['severity'] != 'critical':
            inc['severity'] = 'critical'
        _save(data)
        return inc

    # No non-resolved incident found — carry recurrence history from any prior row
    prior_days = []
    for inc in data['incidents']:
        if inc['key'] == key:
            prior_days = inc.get('days_seen', [])

    days = sorted(set(prior_days + [today]))
    inc = {
        'id':         f'inc_{datetime.now().strftime("%Y%m%d_%H%M%S")}_{hashlib.sha1(key.encode()).hexdigest()[:6]}',
        'key':        key,
        'first_seen': now,
        'last_seen':  now,
        'count':      1,
        'days_seen':  days,
        'wallet':     wallet,
        'platform':   platform,
        'pos_id':     pos_id,
        'signal':     signal,
        'severity':   'critical' if len(days) >= 3 else severity,
        'title':      title or f'{signal} {platform}'.strip(),
        'detail':     detail,
        'status':     'detected',
        'actions':    [],
        'proposal':   None,
    }
    data['incidents'].append(inc)
    if len(data['incidents']) > MAX_INCIDENTS:
        data['incidents'] = data['incidents'][-MAX_INCIDENTS:]
    _save(data)
    return inc


def cleanup_duplicates() -> int:
    """For each incident key:
    - Non-resolved: keep only the most-recent row; merge count + days_seen from older duplicates.
    - Resolved: keep only the most-recent resolved row (1 per key — history without clutter).
    Returns count of rows removed."""
    data = _load()
    seen_open: dict = {}      # key → newest non-resolved incident kept
    seen_resolved: set = set()  # keys for which we already kept 1 resolved row
    to_keep = []

    for inc in reversed(data['incidents']):   # newest first
        key = inc['key']
        if inc['status'] == 'resolved':
            if key not in seen_resolved:
                seen_resolved.add(key)
                to_keep.append(inc)
            # else: older resolved duplicate — drop it
            continue
        # non-resolved
        if key not in seen_open:
            seen_open[key] = inc
            to_keep.append(inc)
        else:
            # Older non-resolved duplicate — merge into keeper, then drop
            keeper = seen_open[key]
            keeper['count'] += inc.get('count', 1)
            for d in inc.get('days_seen', []):
                if d not in keeper['days_seen']:
                    keeper['days_seen'].append(d)
            keeper['days_seen'].sort()
            if len(keeper['days_seen']) >= 3 and keeper['severity'] != 'critical':
                keeper['severity'] = 'critical'

    to_keep.reverse()   # restore chronological order
    purged = len(data['incidents']) - len(to_keep)
    if purged > 0:
        data['incidents'] = to_keep
        _save(data)
    return purged


def open_keys() -> set:
    """Keys of incidents not yet resolved — lets the watcher avoid re-recording."""
    return {i['key'] for i in _load()['incidents'] if i['status'] != 'resolved'}


def set_agent_state(state: str) -> None:
    """idle | watching | working | waiting — drives the dashboard status chip."""
    data = _load()
    data['agent_state'] = state
    _save(data)


def heartbeat() -> None:
    """Bump `updated` so the dashboard can tell the watcher is alive."""
    _save(_load())


def update(incident_id: str, **fields) -> None:
    """Patch an incident (status, actions, proposal, detail). Used in Phase 2/3."""
    data = _load()
    for inc in data['incidents']:
        if inc['id'] == incident_id:
            inc.update(fields)
            inc['last_seen'] = _now()
            break
    _save(data)


def get_all() -> dict:
    """Full store for the dashboard /api/incidents endpoint."""
    return _load()
