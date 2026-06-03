# Phase A — Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add daily health monitoring for all borrow positions, priority-ordered emergency close, and schema migrations for tracking data.

**Architecture:** Three independent changes — (1) new `health_monitor.py` standalone script + importable module; (2) `withdraw_all.py` sort order changed to Borrow → Supply/Lend → LP with a `force_close_all()` callable for future dashboard API; (3) `state.py` `migrate_db()` to add 3 new columns + 2 new tables idempotently.

**Tech Stack:** Python 3.11, SQLite (sqlite3), web3.py, existing borrow modules (compound_borrow, moonwell_borrow, fluid_borrow, aave_borrow), unittest.mock

---

## File Map

| File | Change |
|---|---|
| `state.py` | Add `migrate_db()`, `record_cooldown()`, `get_cooldown_days()` |
| `health_monitor.py` | NEW — `check_all()` + `run()` |
| `withdraw_all.py` | Modify `run()` sort order + add `force_close_all()` |
| `test_health_monitor.py` | NEW — mock-based unit tests |
| `test_state_migration.py` | NEW — SQLite migration tests |

---

## Task 1: state.py — Schema Migration

**Files:**
- Modify: `state.py`
- Test: `test_state_migration.py`

### What to add

Three new columns on `positions` table (nullable, no default — existing rows stay as NULL):
- `opened_usd REAL`
- `closed_usd REAL`
- `gas_cost_wei INTEGER`

Two new tables:
- `daily_stats` — one row per day, counts + USD tracking
- `platform_cooldown` — one row per platform, stores last_closed_at

- [ ] **Step 1: Write the failing test**

Create `test_state_migration.py`:

```python
"""Test state.py schema migration — runs on temp DB, no side effects."""
import os, sqlite3, tempfile, unittest

# Patch DB_PATH before importing state
_tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_tmp.close()
os.environ['STATE_DB_PATH'] = _tmp.name

import state  # import AFTER patching

class TestMigrateDb(unittest.TestCase):

    def setUp(self):
        # Reset to clean slate each test
        conn = sqlite3.connect(_tmp.name)
        conn.execute('DROP TABLE IF EXISTS positions')
        conn.execute('DROP TABLE IF EXISTS daily_stats')
        conn.execute('DROP TABLE IF EXISTS platform_cooldown')
        conn.commit()
        conn.close()

    def test_init_creates_positions_table(self):
        state.init_db()
        conn = sqlite3.connect(_tmp.name)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
        conn.close()
        self.assertIn('id', cols)
        self.assertIn('platform', cols)
        self.assertIn('status', cols)

    def test_migrate_adds_new_columns(self):
        state.init_db()
        state.migrate_db()
        conn = sqlite3.connect(_tmp.name)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()]
        conn.close()
        self.assertIn('opened_usd', cols)
        self.assertIn('closed_usd', cols)
        self.assertIn('gas_cost_wei', cols)

    def test_migrate_creates_daily_stats(self):
        state.init_db()
        state.migrate_db()
        conn = sqlite3.connect(_tmp.name)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        self.assertIn('daily_stats', tables)
        self.assertIn('platform_cooldown', tables)

    def test_migrate_is_idempotent(self):
        state.init_db()
        state.migrate_db()
        # Second call must not raise
        state.migrate_db()

    def test_record_and_get_cooldown(self):
        state.init_db()
        state.migrate_db()
        state.record_cooldown('compound_usdc')
        days = state.get_cooldown_days('compound_usdc')
        self.assertEqual(days, 0)

    def test_cooldown_unknown_platform(self):
        state.init_db()
        state.migrate_db()
        days = state.get_cooldown_days('never_used_platform')
        self.assertEqual(days, 999)

if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

```powershell
cd C:\Users\Admin\base-agent
python test_state_migration.py
```

Expected: `AttributeError: module 'state' has no attribute 'migrate_db'`

- [ ] **Step 3: Implement in state.py**

Open `state.py`. At the top, change:
```python
DB_PATH = os.path.join(os.path.dirname(__file__), 'state.db')
```
to:
```python
DB_PATH = os.environ.get('STATE_DB_PATH', os.path.join(os.path.dirname(__file__), 'state.db'))
```

Then add after `init_db()`:

```python
def migrate_db():
    """Add new columns/tables without losing existing data. Idempotent."""
    with _conn() as c:
        for col_def in ('opened_usd REAL', 'closed_usd REAL', 'gas_cost_wei INTEGER'):
            try:
                c.execute(f'ALTER TABLE positions ADD COLUMN {col_def}')
            except Exception:
                pass  # column already exists
        c.execute('''CREATE TABLE IF NOT EXISTS daily_stats (
            date          TEXT PRIMARY KEY,
            lend_count    INTEGER DEFAULT 0,
            borrow_count  INTEGER DEFAULT 0,
            lp_count      INTEGER DEFAULT 0,
            vote_count    INTEGER DEFAULT 0,
            game_count    INTEGER DEFAULT 0,
            deploy_count  INTEGER DEFAULT 0,
            volume_usd    REAL DEFAULT 0.0,
            portfolio_usd REAL DEFAULT 0.0,
            eth_price     REAL DEFAULT 0.0,
            gas_usd       REAL DEFAULT 0.0
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS platform_cooldown (
            platform_key   TEXT PRIMARY KEY,
            last_closed_at TEXT
        )''')


def record_cooldown(platform_key: str):
    """Record that platform was closed today (for cooldown rule engine)."""
    today = date.today().isoformat()
    with _conn() as c:
        c.execute(
            'INSERT OR REPLACE INTO platform_cooldown (platform_key, last_closed_at) VALUES (?,?)',
            (platform_key, today)
        )


def get_cooldown_days(platform_key: str) -> int:
    """Days since platform was last closed. 999 if never closed."""
    with _conn() as c:
        row = c.execute(
            'SELECT last_closed_at FROM platform_cooldown WHERE platform_key=?',
            (platform_key,)
        ).fetchone()
    if not row:
        return 999
    return (date.today() - date.fromisoformat(row[0])).days
```

Also update `init_db()` to call `migrate_db()` at the end:

```python
def init_db():
    with _conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS positions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            platform    TEXT NOT NULL,
            token       TEXT NOT NULL,
            amount_wei  TEXT NOT NULL,
            entry_date  TEXT NOT NULL,
            expiry_date TEXT NOT NULL,
            tx_hash     TEXT,
            status      TEXT DEFAULT 'active'
        )''')
    migrate_db()
```

- [ ] **Step 4: Run tests to verify they pass**

```powershell
python test_state_migration.py -v
```

Expected: `6 tests ... OK`

- [ ] **Step 5: Run migrate on live DB to verify no data loss**

```powershell
python -c "import state; state.init_db(); print('migrate OK')"
python -c "import state; print(len(state.get_active()), 'active positions still intact')"
```

- [ ] **Step 6: Commit**

```powershell
git add state.py test_state_migration.py
git commit -m "feat(state): add migrate_db with opened_usd/closed_usd/gas_cost_wei + daily_stats + platform_cooldown tables"
```

---

## Task 2: health_monitor.py — New File

**Files:**
- Create: `health_monitor.py`
- Test: `test_health_monitor.py`

Health thresholds (match per-module HEALTH_CLOSE_THRESHOLD = 1.5):

| Status | Range |
|---|---|
| `OK` | health >= 1.5 |
| `WARNING` | 1.2 <= health < 1.5 |
| `CRITICAL` | health < 1.2 |

The four borrow modules and their `check_health(encoded_state, p_cfg)` signatures:
- `compound_borrow.check_health(encoded: str, p: dict) -> float`
- `moonwell_borrow.check_health(encoded: str, p: dict) -> float`
- `fluid_borrow.check_health(encoded: str, p: dict) -> float`
- `aave_borrow.check_health(encoded_state: str, p: dict) -> float`

`state.get_active()` returns rows as tuples:
`(id, platform, token, amount_wei_str, entry_date, expiry_date, tx_hash, status)`

- [ ] **Step 1: Write the failing test**

Create `test_health_monitor.py`:

```python
"""Unit tests for health_monitor.py — all web3/chain calls mocked."""
import unittest
from unittest.mock import patch, MagicMock

# Stub executor before health_monitor imports it (avoids web3 connection at import time)
import sys
sys.modules.setdefault('executor', MagicMock())
sys.modules.setdefault('swap', MagicMock())
sys.modules.setdefault('web3', MagicMock())

import health_monitor

# Fake positions row: (id, platform, token, amount_wei_str, entry, expiry, tx_hash, status)
def _pos(pos_id, platform, ptype, amount='encoded_state'):
    return (pos_id, platform, 'USDC', amount, '2026-05-30', '2026-06-05', '0xabc', 'active')

# Minimal platform config keyed by platform name
_CFG = {
    'platforms': {
        'cb_usdc_weth':   {'type': 'compound_borrow', 'display_name': 'WETH->USDC [Compound]'},
        'mw_weth_usdc':   {'type': 'mw_borrow',       'display_name': 'WETH->USDC [Moonwell]'},
        'fl_eth_usdc':    {'type': 'fluid_borrow',    'display_name': 'ETH->USDC [Fluid]'},
        'aave_weth_usdc': {'type': 'aave_borrow',     'display_name': 'WETH->USDC [AAVE]'},
        'compound_usdc':  {'type': 'comet',            'display_name': 'Compound Supply'},
    }
}


class TestHealthStatus(unittest.TestCase):

    def test_status_ok(self):
        self.assertEqual(health_monitor._status(2.0), 'OK')

    def test_status_ok_at_threshold(self):
        self.assertEqual(health_monitor._status(1.5), 'OK')

    def test_status_warning(self):
        self.assertEqual(health_monitor._status(1.3), 'WARNING')

    def test_status_critical(self):
        self.assertEqual(health_monitor._status(1.1), 'CRITICAL')

    def test_status_critical_zero(self):
        self.assertEqual(health_monitor._status(0.0), 'CRITICAL')


class TestCheckAll(unittest.TestCase):

    def _run(self, positions, health_values):
        """positions: list of fake rows; health_values: {platform: float}"""
        with patch.object(health_monitor.state, 'get_active', return_value=positions), \
             patch.dict(health_monitor.CFG, _CFG), \
             patch.object(health_monitor._compound_borrow, 'check_health',
                          side_effect=lambda enc, p: health_values.get(p.get('display_name', ''), 999.0)), \
             patch.object(health_monitor._mw_borrow, 'check_health',
                          side_effect=lambda enc, p: health_values.get(p.get('display_name', ''), 999.0)), \
             patch.object(health_monitor._fl_borrow, 'check_health',
                          side_effect=lambda enc, p: health_values.get(p.get('display_name', ''), 999.0)), \
             patch.object(health_monitor._aave_borrow, 'check_health',
                          side_effect=lambda enc, p: health_values.get(p.get('display_name', ''), 999.0)):
            return health_monitor.check_all()

    def test_skips_non_borrow_positions(self):
        positions = [_pos(1, 'compound_usdc', 'comet')]
        results = self._run(positions, {})
        self.assertEqual(results, [])

    def test_compound_borrow_checked(self):
        positions = [_pos(1, 'cb_usdc_weth', 'compound_borrow')]
        results = self._run(positions, {'WETH->USDC [Compound]': 2.0})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['platform'], 'cb_usdc_weth')
        self.assertEqual(results[0]['ptype'], 'compound_borrow')
        self.assertAlmostEqual(results[0]['health'], 2.0)
        self.assertEqual(results[0]['status'], 'OK')

    def test_mw_borrow_checked(self):
        positions = [_pos(2, 'mw_weth_usdc', 'mw_borrow')]
        results = self._run(positions, {'WETH->USDC [Moonwell]': 1.3})
        self.assertEqual(results[0]['status'], 'WARNING')

    def test_fluid_borrow_checked(self):
        positions = [_pos(3, 'fl_eth_usdc', 'fluid_borrow')]
        results = self._run(positions, {'ETH->USDC [Fluid]': 1.1})
        self.assertEqual(results[0]['status'], 'CRITICAL')

    def test_aave_borrow_checked(self):
        positions = [_pos(4, 'aave_weth_usdc', 'aave_borrow')]
        results = self._run(positions, {'WETH->USDC [AAVE]': 1.5})
        self.assertEqual(results[0]['status'], 'OK')

    def test_all_four_protocols_in_one_run(self):
        positions = [
            _pos(1, 'cb_usdc_weth',   'compound_borrow'),
            _pos(2, 'mw_weth_usdc',   'mw_borrow'),
            _pos(3, 'fl_eth_usdc',    'fluid_borrow'),
            _pos(4, 'aave_weth_usdc', 'aave_borrow'),
        ]
        health_vals = {
            'WETH->USDC [Compound]': 2.5,
            'WETH->USDC [Moonwell]': 1.4,
            'ETH->USDC [Fluid]':     1.1,
            'WETH->USDC [AAVE]':     3.0,
        }
        results = self._run(positions, health_vals)
        self.assertEqual(len(results), 4)
        statuses = {r['platform']: r['status'] for r in results}
        self.assertEqual(statuses['cb_usdc_weth'],   'OK')
        self.assertEqual(statuses['mw_weth_usdc'],   'WARNING')
        self.assertEqual(statuses['fl_eth_usdc'],    'CRITICAL')
        self.assertEqual(statuses['aave_weth_usdc'], 'OK')

    def test_check_error_returns_error_status(self):
        positions = [_pos(1, 'cb_usdc_weth', 'compound_borrow')]
        with patch.object(health_monitor.state, 'get_active', return_value=positions), \
             patch.dict(health_monitor.CFG, _CFG), \
             patch.object(health_monitor._compound_borrow, 'check_health',
                          side_effect=Exception('RPC timeout')):
            results = health_monitor.check_all()
        self.assertEqual(results[0]['status'], 'ERROR')

if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

```powershell
python test_health_monitor.py
```

Expected: `ModuleNotFoundError: No module named 'health_monitor'`

- [ ] **Step 3: Create health_monitor.py**

```python
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
    results = []
    for pos in state.get_active():
        pos_id, platform, _token, encoded, _entry, _expiry, _txh, _status_col = pos
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
        flag = {'OK': '', 'WARNING': '  << WARN', 'CRITICAL': '  << CRITICAL', 'ERROR': '  << ERROR'}[r['status']]
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
```

- [ ] **Step 4: Run tests to verify they pass**

```powershell
python test_health_monitor.py -v
```

Expected: `10 tests ... OK`

- [ ] **Step 5: Smoke test against live state.db**

```powershell
python health_monitor.py
```

Expected: table printed with all active borrow positions, no crash. If no active borrows: `No active borrow positions.`

- [ ] **Step 6: Commit**

```powershell
git add health_monitor.py test_health_monitor.py
git commit -m "feat: add health_monitor.py - daily health check for all borrow positions (compound/moonwell/fluid/aave)"
```

---

## Task 3: withdraw_all.py — Priority Ordering

**Files:**
- Modify: `withdraw_all.py`

Current behavior: `positions = sorted(state.get_active(), key=lambda r: r[0])` — sorts by ID (oldest first).

New behavior: sort by type priority first (Borrow=0, Supply=1, LP=2, Other=3), then by ID within same priority.

Platform type categories:
- **Borrow (0)**: `compound_borrow`, `mw_borrow`, `fluid_borrow`, `aave_borrow`
- **Supply (1)**: `comet`, `erc4626`, `ctoken`, `psm_hold`, `beefy_single`, `aave_supply`
- **LP (2)**: `beefy_lp`, `aero_lp`, `uni_lp`, `pancake_lp`
- **Other (3)**: `aero_vote`, anything unknown

- [ ] **Step 1: Write the failing test**

Add to a new file `test_withdraw_priority.py`:

```python
"""Test withdraw_all.py priority ordering logic in isolation."""
import unittest

# Define the exact priority logic to be implemented
_BORROW_TYPES  = {'compound_borrow', 'mw_borrow', 'fluid_borrow', 'aave_borrow'}
_SUPPLY_TYPES  = {'comet', 'erc4626', 'ctoken', 'psm_hold', 'beefy_single', 'aave_supply'}
_LP_TYPES      = {'beefy_lp', 'aero_lp', 'uni_lp', 'pancake_lp'}

_PLATFORMS_CFG = {
    'cb_usdc_weth':   {'type': 'compound_borrow'},
    'mw_weth_usdc':   {'type': 'mw_borrow'},
    'fl_eth_usdc':    {'type': 'fluid_borrow'},
    'aave_weth_usdc': {'type': 'aave_borrow'},
    'compound_usdc':  {'type': 'comet'},
    'fluid_usdc':     {'type': 'erc4626'},
    'aero_lp_weth':   {'type': 'aero_lp'},
    'uni_lp_weth':    {'type': 'uni_lp'},
    'aero_vote':      {'type': 'aero_vote'},
}

def _type_priority(pos_row, platforms_cfg):
    ptype = platforms_cfg.get(pos_row[1], {}).get('type', '')
    if ptype in _BORROW_TYPES: return (0, pos_row[0])
    if ptype in _SUPPLY_TYPES: return (1, pos_row[0])
    if ptype in _LP_TYPES:     return (2, pos_row[0])
    return (3, pos_row[0])

def _make_pos(pos_id, platform):
    return (pos_id, platform, 'USDC', '5000000', '2026-05-30', '2026-06-05', '0xabc', 'active')

class TestWithdrawPriority(unittest.TestCase):

    def _sort(self, rows):
        return sorted(rows, key=lambda r: _type_priority(r, _PLATFORMS_CFG))

    def test_borrow_before_supply(self):
        rows = [
            _make_pos(1, 'compound_usdc'),  # supply
            _make_pos(2, 'cb_usdc_weth'),   # borrow
        ]
        sorted_rows = self._sort(rows)
        self.assertEqual(sorted_rows[0][1], 'cb_usdc_weth')   # borrow first
        self.assertEqual(sorted_rows[1][1], 'compound_usdc')  # supply second

    def test_borrow_before_lp(self):
        rows = [
            _make_pos(1, 'aero_lp_weth'),  # lp
            _make_pos(2, 'fl_eth_usdc'),   # borrow
        ]
        sorted_rows = self._sort(rows)
        self.assertEqual(sorted_rows[0][1], 'fl_eth_usdc')

    def test_supply_before_lp(self):
        rows = [
            _make_pos(1, 'uni_lp_weth'),  # lp
            _make_pos(2, 'fluid_usdc'),   # supply
        ]
        sorted_rows = self._sort(rows)
        self.assertEqual(sorted_rows[0][1], 'fluid_usdc')

    def test_lp_before_vote(self):
        rows = [
            _make_pos(1, 'aero_vote'),     # other
            _make_pos(2, 'aero_lp_weth'),  # lp
        ]
        sorted_rows = self._sort(rows)
        self.assertEqual(sorted_rows[0][1], 'aero_lp_weth')

    def test_within_same_priority_id_ascending(self):
        rows = [
            _make_pos(5, 'mw_weth_usdc'),   # borrow id=5
            _make_pos(2, 'cb_usdc_weth'),   # borrow id=2
            _make_pos(8, 'aave_weth_usdc'), # borrow id=8
        ]
        sorted_rows = self._sort(rows)
        ids = [r[0] for r in sorted_rows]
        self.assertEqual(ids, [2, 5, 8])  # sorted by id within priority

    def test_all_four_priorities(self):
        rows = [
            _make_pos(4, 'aero_vote'),      # other=3
            _make_pos(3, 'uni_lp_weth'),    # lp=2
            _make_pos(2, 'compound_usdc'),  # supply=1
            _make_pos(1, 'cb_usdc_weth'),   # borrow=0
        ]
        sorted_rows = self._sort(rows)
        platforms = [r[1] for r in sorted_rows]
        self.assertEqual(platforms, ['cb_usdc_weth', 'compound_usdc', 'uni_lp_weth', 'aero_vote'])

if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run to verify the standalone logic works**

```powershell
python test_withdraw_priority.py -v
```

Expected: `6 tests ... OK` (tests validate the logic independently before we add to withdraw_all.py)

- [ ] **Step 3: Modify withdraw_all.py**

Open `withdraw_all.py`. After the existing constants (after `AERO_STABLE_ONLY_TOKENS = {...}`), add:

```python
# Priority ordering: Borrow=0 (close first to stop debt accrual) → Supply=1 → LP=2 → Other=3
_BORROW_TYPES = {'compound_borrow', 'mw_borrow', 'fluid_borrow', 'aave_borrow'}
_SUPPLY_TYPES = {'comet', 'erc4626', 'ctoken', 'psm_hold', 'beefy_single', 'aave_supply'}
_LP_TYPES     = {'beefy_lp', 'aero_lp', 'uni_lp', 'pancake_lp'}


def _type_priority(pos_row: tuple) -> tuple:
    """Sort key: (priority_group, pos_id). Lower group = withdraw first."""
    ptype = CFG['platforms'].get(pos_row[1], {}).get('type', '')
    if ptype in _BORROW_TYPES: return (0, pos_row[0])
    if ptype in _SUPPLY_TYPES: return (1, pos_row[0])
    if ptype in _LP_TYPES:     return (2, pos_row[0])
    return (3, pos_row[0])
```

In the `run()` function, replace:

```python
positions = sorted(state.get_active(), key=lambda r: r[0])  # sort by id ASC
```

with:

```python
positions = sorted(state.get_active(), key=_type_priority)  # Borrow -> Supply -> LP -> Other
```

- [ ] **Step 4: Add force_close_all() function**

At the end of `withdraw_all.py`, before `if __name__ == '__main__':`, add:

```python
def force_close_all():
    """
    Emergency close — same as run() but callable programmatically.
    Used by serve_dashboard.py /api/emergency_close endpoint (Phase D).
    """
    run()
```

And update `if __name__ == '__main__':` to:

```python
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--emergency', action='store_true',
                        help='Force close all positions (priority order)')
    args = parser.parse_args()
    run()
```

(Both `--emergency` and plain invocation call `run()` — the flag is a semantic marker for future logging/alerting use.)

- [ ] **Step 5: Verify DRY_RUN still works**

```powershell
$env:DRY_RUN='true'; python withdraw_all.py
```

Expected: runs with `[DRY RUN]` mode, prints priority-sorted positions in order (Borrow first if any active), no TX sent.

- [ ] **Step 6: Commit**

```powershell
git add withdraw_all.py test_withdraw_priority.py
git commit -m "feat(withdraw): priority-ordered close (Borrow->Supply->LP->Other) + force_close_all() for dashboard API"
```

---

## Task 4: Integration — agent.py calls health_monitor

**Files:**
- Modify: `agent.py`

Currently `agent.py` has `_check_borrow_health()` inline (lines ~219-287) with duplicated logic for each borrow type. Replace it with a call to `health_monitor.check_all()` — same behavior, centralized.

- [ ] **Step 1: Add import to agent.py**

In `agent.py`, after the existing imports add:

```python
import health_monitor as _health_monitor
```

- [ ] **Step 2: Replace _check_borrow_health() body**

Find the existing `_check_borrow_health(failed)` function (starts ~line 219). Replace the entire function body with:

```python
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

        threshold = 1.5
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
                elif ptype == 'mw_borrow':
                    _mw_borrow.close_borrow(encoded, p, pos_id)
                elif ptype == 'fluid_borrow':
                    txh = _fl_borrow.close_borrow(encoded, p)
                    state.close_position(pos_id)
                elif ptype == 'aave_borrow':
                    txh = _aave_borrow.close_borrow(encoded, p)
                    state.close_position(pos_id)
                log.info(f'Early closed {_pname(platform, p)}')
            except Exception as e:
                log.error(f'Early close failed [{_pname(platform, p)}]: {e}')
                failed.append(f'early_close_{platform}')
```

- [ ] **Step 3: Verify agent.py imports cleanly**

```powershell
python -c "import agent; print('agent import OK')"
```

Expected: `agent import OK` (no crash)

- [ ] **Step 4: Commit**

```powershell
git add agent.py
git commit -m "refactor(agent): replace inline _check_borrow_health with health_monitor.check_all()"
```

---

## Self-Review

### Spec coverage

| Spec Requirement | Task |
|---|---|
| `health_monitor.py` — all 4 borrow protocols | Task 2 |
| `withdraw_all.py` priority order Borrow→Lend→LP | Task 3 |
| Emergency Close callable | Task 3 (`force_close_all()`) |
| `state.db` opened_usd / closed_usd / gas_cost_wei | Task 1 |
| `daily_stats` table | Task 1 |
| `platform_cooldown` table | Task 1 |

All 6 spec requirements covered. ✅

### Placeholder scan

No TBD/TODO/similar patterns. All code blocks complete. ✅

### Type consistency

- `check_all()` returns `list[dict]` with keys `pos_id, platform, ptype, health, status, encoded` — used consistently in Task 2 (health_monitor tests) and Task 4 (agent.py integration). ✅
- `_type_priority(pos_row)` takes `tuple` (state.get_active() row format) — consistent with `withdraw_all.py` usage. ✅
- `migrate_db()` is called from `init_db()` — any code calling `state.init_db()` automatically gets migration. ✅
- `STATE_DB_PATH` env override in state.py — needed for test isolation without patching the module. ✅
