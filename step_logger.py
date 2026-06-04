"""step_logger.py — shared per-step action logger for base-agent.

Any module can call slog() to append a sub-step entry to action_log.json.
set_context() must be called once per platform action (done by agent._action_log).
"""

import os, json, logging
from datetime import datetime, date

log = logging.getLogger(__name__)

_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')
_LOG_MAX   = 200

def _get_log_file() -> str:
    wid = os.environ.get('WALLET_ID', 'default')
    return os.path.join(_CACHE_DIR, f'action_log_{wid}.json')

_ctx: dict = {'platform': '', 'display_name': ''}


def set_context(platform: str, display_name: str = '') -> None:
    """Set current platform context. Call once per action before sub-steps fire."""
    _ctx['platform']     = platform
    _ctx['display_name'] = display_name or platform


def slog(step: str, detail: str,
         txhash: str | None = None, usd_est: float | None = None) -> None:
    """Append a sub-step entry to action_log.json using the current context."""
    if not _ctx['platform']:
        return  # no context set — skip silently
    entry = {
        'ts':           datetime.now().strftime('%H:%M:%S'),
        'date':         date.today().isoformat(),
        'platform':     _ctx['platform'],
        'display_name': _ctx['display_name'],
        'step':         step,
        'detail':       detail,
        'txhash':       txhash,
        'usd_est':      usd_est,
    }
    try:
        _lf = _get_log_file()
        os.makedirs(_CACHE_DIR, exist_ok=True)
        try:
            with open(_lf) as f:
                entries = json.load(f)
        except Exception:
            entries = []
        entries.append(entry)
        if len(entries) > _LOG_MAX:
            entries = entries[-_LOG_MAX:]
        with open(_lf, 'w') as f:
            json.dump(entries, f, indent=2)
    except Exception as e:
        log.warning(f'step_logger.slog failed: {e}')
