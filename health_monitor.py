"""
health_monitor.py — Daily health check for all active borrow positions.

Queries health factor from all 4 borrow protocols:
  compound_borrow -> compound_borrow.check_health(encoded, p) -> float
  mw_borrow       -> moonwell_borrow.check_health(encoded, p) -> float
  fluid_borrow    -> fluid_borrow.check_health(encoded, p)    -> float
  aave_borrow     -> aave_borrow.check_health(encoded, p)     -> float

Health thresholds:
  >= 1.5  OK
  1.2-1.5 WARNING
  < 1.2   CRITICAL

Usage (standalone):
    python health_monitor.py

Programmatic:
    from health_monitor import check_all
    results = check_all()   # list[dict]
"""

import os, json, logging, sys
from dotenv import load_dotenv

load_dotenv()

import state
import compound_borrow as _compound_borrow
import moonwell_borrow as _mw_borrow
import fluid_borrow    as _fl_borrow
import aave_borrow     as _aave_borrow

log = logging.getLogger(__name__)

with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as f:
    CFG = json.load(f)

HEALTH_OK   = 1.5
HEALTH_WARN = 1.2

_BORROW_MODULES = {
    'compound_borrow': _compound_borrow,
    'mw_borrow':       _mw_borrow,
    'fluid_borrow':    _fl_borrow,
    'aave_borrow':     _aave_borrow,
}


def _status(health: float) -> str:
    if health >= HEALTH_OK:
        return 'OK'
    if health >= HEALTH_WARN:
        return 'WARNING'
    return 'CRITICAL'


def check_all() -> list:
    """
    Check health for all active borrow positions.

    Returns list of dicts:
      {
        'pos_id':   int,
        'platform': str,
        'ptype':    str,     # compound_borrow | mw_borrow | fluid_borrow | aave_borrow
        'health':   float,
        'status':   str,     # OK | WARNING | CRITICAL | ERROR
        'encoded':  str,     # raw amount_wei_str from state.db
      }
    """
    state.init_db()
    results = []
    for pos in state.get_active():
        # Unpack only the first 8 columns; schema may have extra columns (opened_usd, etc.)
        pos_id, platform, _token, encoded = pos[0], pos[1], pos[2], pos[3]
        p = CFG['platforms'].get(platform, {})
        ptype = p.get('type', '')
        if ptype not in _BORROW_MODULES:
            continue
        mod = _BORROW_MODULES[ptype]
        try:
            health = mod.check_health(encoded, p)
            results.append({
                'pos_id':   pos_id,
                'platform': platform,
                'ptype':    ptype,
                'health':   health,
                'status':   _status(health),
                'encoded':  encoded,
            })
        except Exception as e:
            log.warning(f'health check failed [{platform}]: {e}')
            results.append({
                'pos_id':   pos_id,
                'platform': platform,
                'ptype':    ptype,
                'health':   0.0,
                'status':   'ERROR',
                'encoded':  encoded,
            })
    return results


def run() -> list:
    """Standalone entry point — prints formatted table and returns results."""
    state.init_db()
    results = check_all()

    if not results:
        print('No active borrow positions.')
        return results

    print()
    print('=' * 68)
    print('BORROW HEALTH MONITOR')
    print(f'  {"ID":>3}  {"Platform":28}  {"Type":16}  {"Health":>8}  Status')
    print('  ' + '-' * 64)
    for r in results:
        name = CFG['platforms'].get(r['platform'], {}).get('display_name', r['platform'])
        flag = {'OK': '', 'WARNING': '  << WARN', 'CRITICAL': '  << CRITICAL', 'ERROR': '  << ERROR'}.get(r['status'], '')
        print(f'  {r["pos_id"]:>3}  {name:28}  {r["ptype"]:16}  {r["health"]:>8.2f}  {r["status"]}{flag}')
    print('  ' + '-' * 64)

    ok       = sum(1 for r in results if r['status'] == 'OK')
    warnings = sum(1 for r in results if r['status'] == 'WARNING')
    critical = sum(1 for r in results if r['status'] in ('CRITICAL', 'ERROR'))
    print(f'  {ok} OK  |  {warnings} WARNING  |  {critical} CRITICAL/ERROR')
    print('=' * 68)
    print()
    return results


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout)])
    run()
