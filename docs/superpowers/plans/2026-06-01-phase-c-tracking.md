# Phase C — Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add daily action counting, portfolio USD snapshot, and weekly summary report to the Base airdrop agent.

**Architecture:** Three layers — (1) state.py gains write helpers for daily_stats table (already migrated); (2) agent.py calls those helpers after each successful action; (3) two new standalone scripts: `portfolio_tracker.py` snapshots wallet+position USD each day, `weekly_report.py` summarises the last 7 days every Monday.

**Tech Stack:** Python stdlib + sqlite3 (via state.py), executor.py price/balance functions (get_eth_balance, get_eth_usd_price, get_token_balance), APScheduler (existing in agent.py).

---

## File Map

| File | Change | Responsibility |
|---|---|---|
| `state.py` | Modify | Add `log_daily_stat(category)`, `update_daily_portfolio(portfolio_usd, eth_price)`, `get_daily_stats(days)` |
| `test_daily_stats.py` | Create | Unit tests for new state.py functions (temp DB, no web3) |
| `agent.py` | Modify | Call `state.log_daily_stat(cat)` after every successful action open |
| `portfolio_tracker.py` | Create | Compute wallet+position USD snapshot, call `state.update_daily_portfolio` |
| `test_portfolio_tracker.py` | Create | Unit tests with mocked executor |
| `weekly_report.py` | Create | Read last 7 days from daily_stats, print/return formatted report |
| `test_weekly_report.py` | Create | Unit tests with pre-seeded temp DB |

---

## Task 1: state.py — daily_stats write/read helpers

**Files:**
- Modify: `state.py`
- Create: `test_daily_stats.py`

**Context:** `daily_stats` table already exists (created by `migrate_db()`). Schema:
```sql
date TEXT PRIMARY KEY, lend_count INTEGER DEFAULT 0, borrow_count INTEGER DEFAULT 0,
lp_count INTEGER DEFAULT 0, vote_count INTEGER DEFAULT 0, game_count INTEGER DEFAULT 0,
deploy_count INTEGER DEFAULT 0, volume_usd REAL DEFAULT 0.0,
portfolio_usd REAL DEFAULT 0.0, eth_price REAL DEFAULT 0.0, gas_usd REAL DEFAULT 0.0
```

Category → column map: `lend→lend_count`, `borrow→borrow_count`, `lp→lp_count`, `vote→vote_count`, `game→game_count`, `deploy→deploy_count`.

- [ ] **Step 1: Write failing tests**

Create `C:\Users\Admin\base-agent\test_daily_stats.py`:

```python
"""Unit tests for daily_stats write/read helpers in state.py."""
import os, sqlite3, tempfile, unittest
from datetime import date, timedelta

_tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_tmp.close()
os.environ['STATE_DB_PATH'] = _tmp.name

import state


def _reset():
    conn = sqlite3.connect(_tmp.name)
    conn.execute('DROP TABLE IF EXISTS positions')
    conn.execute('DROP TABLE IF EXISTS daily_stats')
    conn.execute('DROP TABLE IF EXISTS platform_cooldown')
    conn.commit()
    conn.close()


class TestLogDailyStat(unittest.TestCase):

    def setUp(self):
        _reset()
        state.init_db()

    def test_first_log_creates_row(self):
        state.log_daily_stat('lend')
        rows = state.get_daily_stats(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['lend_count'], 1)

    def test_multiple_logs_increment(self):
        state.log_daily_stat('lend')
        state.log_daily_stat('lend')
        state.log_daily_stat('borrow')
        rows = state.get_daily_stats(1)
        self.assertEqual(rows[0]['lend_count'], 2)
        self.assertEqual(rows[0]['borrow_count'], 1)

    def test_all_categories(self):
        for cat in ('lend', 'borrow', 'lp', 'vote', 'game', 'deploy'):
            state.log_daily_stat(cat)
        rows = state.get_daily_stats(1)
        r = rows[0]
        for col in ('lend_count', 'borrow_count', 'lp_count', 'vote_count', 'game_count', 'deploy_count'):
            self.assertEqual(r[col], 1)

    def test_unknown_category_raises(self):
        with self.assertRaises(ValueError):
            state.log_daily_stat('bogus')


class TestUpdateDailyPortfolio(unittest.TestCase):

    def setUp(self):
        _reset()
        state.init_db()

    def test_update_sets_values(self):
        state.update_daily_portfolio(1234.56, 3500.0)
        rows = state.get_daily_stats(1)
        self.assertAlmostEqual(rows[0]['portfolio_usd'], 1234.56, places=2)
        self.assertAlmostEqual(rows[0]['eth_price'], 3500.0, places=2)

    def test_update_is_idempotent(self):
        state.update_daily_portfolio(100.0, 3000.0)
        state.update_daily_portfolio(200.0, 3100.0)
        rows = state.get_daily_stats(1)
        self.assertAlmostEqual(rows[0]['portfolio_usd'], 200.0, places=2)


class TestGetDailyStats(unittest.TestCase):

    def setUp(self):
        _reset()
        state.init_db()

    def test_returns_empty_list_if_no_rows(self):
        rows = state.get_daily_stats(7)
        self.assertEqual(rows, [])

    def test_respects_days_limit(self):
        # Manually insert rows older than requested window
        conn = sqlite3.connect(_tmp.name)
        old = (date.today() - timedelta(days=10)).isoformat()
        conn.execute(
            "INSERT INTO daily_stats (date, lend_count) VALUES (?, ?)", (old, 5)
        )
        conn.commit()
        conn.close()
        rows = state.get_daily_stats(7)
        self.assertEqual(rows, [])

    def test_returns_rows_newest_first(self):
        conn = sqlite3.connect(_tmp.name)
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        conn.execute("INSERT INTO daily_stats (date, lend_count) VALUES (?, ?)", (today, 2))
        conn.execute("INSERT INTO daily_stats (date, lend_count) VALUES (?, ?)", (yesterday, 1))
        conn.commit()
        conn.close()
        rows = state.get_daily_stats(7)
        self.assertEqual(rows[0]['date'], today)
        self.assertEqual(rows[1]['date'], yesterday)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run tests — verify they FAIL**

```powershell
cd C:\Users\Admin\base-agent
python -m pytest test_daily_stats.py -v 2>&1 | head -40
```

Expected: `AttributeError: module 'state' has no attribute 'log_daily_stat'`

- [ ] **Step 3: Implement the three functions in state.py**

Add after the `get_last_entry_date` function at the bottom of `state.py`:

```python
_STAT_COLS = {
    'lend':   'lend_count',
    'borrow': 'borrow_count',
    'lp':     'lp_count',
    'vote':   'vote_count',
    'game':   'game_count',
    'deploy': 'deploy_count',
}

def log_daily_stat(category: str):
    """Increment today's action counter. category must be in _STAT_COLS."""
    col = _STAT_COLS.get(category)
    if col is None:
        raise ValueError(f'Unknown daily_stat category: {category!r}. Valid: {list(_STAT_COLS)}')
    today = date.today().isoformat()
    with _conn() as c:
        c.execute(
            f'INSERT INTO daily_stats (date, {col}) VALUES (?, 1) '
            f'ON CONFLICT(date) DO UPDATE SET {col} = {col} + 1',
            (today,)
        )

def update_daily_portfolio(portfolio_usd: float, eth_price: float):
    """Overwrite today's portfolio_usd and eth_price snapshot."""
    today = date.today().isoformat()
    with _conn() as c:
        c.execute(
            'INSERT INTO daily_stats (date, portfolio_usd, eth_price) VALUES (?, ?, ?) '
            'ON CONFLICT(date) DO UPDATE SET portfolio_usd=excluded.portfolio_usd, eth_price=excluded.eth_price',
            (today, portfolio_usd, eth_price)
        )

def get_daily_stats(days: int = 7) -> list:
    """Return up to `days` rows from daily_stats, newest first, as list of dicts."""
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            'SELECT * FROM daily_stats WHERE date >= ? ORDER BY date DESC',
            (cutoff,)
        ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests — verify they PASS**

```powershell
cd C:\Users\Admin\base-agent
python -m pytest test_daily_stats.py -v
```

Expected: all 9 tests PASS.

- [ ] **Step 5: Commit**

```powershell
cd C:\Users\Admin\base-agent
git add state.py test_daily_stats.py
git commit -m "feat(state): add log_daily_stat, update_daily_portfolio, get_daily_stats"
```

---

## Task 2: agent.py — log action counts after each successful open

**Files:**
- Modify: `agent.py`

**Context:** In `daily_job()`, every successful `state.add_position()` call corresponds to an action. Need to call `state.log_daily_stat(category)` right after. Category comes from platform type:

| Platform type | category |
|---|---|
| compound_borrow, mw_borrow, fluid_borrow, aave_borrow | `'borrow'` |
| uni_lp, pancake_lp, aero_lp | `'lp'` |
| comet, erc4626, ctoken, psm_hold, beefy_single, beefy_lp, aave_supply | `'lend'` |

Periodic actions: megapot → `'game'`, deploy_contract → `'deploy'`, aero_vote revote → `'vote'`.

- [ ] **Step 1: Add stat calls for borrow opens**

In `daily_job()`, after each borrow `state.add_position(...)` inside the `to_open` loop, add `state.log_daily_stat('borrow')`. Four locations — one per borrow type. Find the pattern:

```python
# compound_borrow block — after:
state.add_position(platform, p.get('borrow_token', 'USDC'), encoded, expiry_days, txh)
today_opened_protocols.add(protocol)
log.info(f'Opened {_pname(platform, p)} -> {txh}')
# ADD:
state.log_daily_stat('borrow')
```

Apply the same pattern to the mw_borrow, fluid_borrow, and aave_borrow blocks (each has a `state.add_position` followed by `today_opened_protocols.add` and `log.info`).

- [ ] **Step 2: Add stat calls for LP opens**

In the `uni_lp / pancake_lp` block, after:
```python
state.add_position(platform, 'LP', str(token_id), expiry_days, txh)
today_opened_protocols.add(protocol)
log.info(f'Opened {platform} tokenId={token_id} -> {txh}')
# ADD:
state.log_daily_stat('lp')
```

- [ ] **Step 3: Add stat calls for generic supply (lend or aero_lp)**

At the bottom of the `to_open` loop, after:
```python
state.add_position(platform, p['token'], amt, expiry_days, txh)
today_opened_protocols.add(protocol)
log.info(f'Opened {platform} -> {txh}')
```
Add:
```python
# aero_lp counts as lp, everything else is lend
state.log_daily_stat('lp' if p['type'] == 'aero_lp' else 'lend')
```

- [ ] **Step 4: Add stat calls for periodic actions**

In `_run_periodic_actions()`, after megapot success:
```python
log.info(f'megapot done  mode={mode}  tx={txh}')
# ADD:
if not _megapot.DRY_RUN:
    state.log_daily_stat('game')
```

After deploy success:
```python
log.info(f'deploy_contract done  name={name}  addr={addr}  tx={txh}')
# ADD:
if not _deploy.DRY_RUN:
    state.log_daily_stat('deploy')
```

In the aero_vote revote branch, after:
```python
log.info(f'aero_vote revote done -> {txh}')
# ADD (only if txh is truthy — revote returns None when skipped):
if txh:
    state.log_daily_stat('vote')
```

- [ ] **Step 5: Smoke test (dry run — verify no crash)**

```powershell
cd C:\Users\Admin\base-agent
DRY_RUN=true python -c "import agent; print('import OK')"
```

Expected: `import OK`

- [ ] **Step 6: Commit**

```powershell
cd C:\Users\Admin\base-agent
git add agent.py
git commit -m "feat(agent): log daily action counts (lend/borrow/lp/vote/game/deploy) to daily_stats"
```

---

## Task 3: portfolio_tracker.py — daily USD snapshot

**Files:**
- Create: `portfolio_tracker.py`
- Create: `test_portfolio_tracker.py`

**Approximation strategy:**
- **Wallet ETH**: `executor.get_eth_balance()` × `executor.get_eth_usd_price()`
- **Wallet USDC**: `executor.get_token_balance(USDC_ADDR, 6)`
- **Active lend/LP positions**: count × $5.0 (each position is ~$5 by design)
- **Active borrow positions**: count × $5.0 (collateral ~$5–14, borrow ~$1–3; rough net ~$5 per position)
- **Total**: wallet_eth_usd + wallet_usdc + lend_lp_usd + borrow_usd

- [ ] **Step 1: Write failing test**

Create `C:\Users\Admin\base-agent\test_portfolio_tracker.py`:

```python
"""Unit tests for portfolio_tracker.py — executor mocked, temp DB."""
import os, sqlite3, tempfile, unittest
from unittest.mock import patch

_tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_tmp.close()
os.environ['STATE_DB_PATH'] = _tmp.name


def _reset():
    conn = sqlite3.connect(_tmp.name)
    for t in ('positions', 'daily_stats', 'platform_cooldown'):
        conn.execute(f'DROP TABLE IF EXISTS {t}')
    conn.commit()
    conn.close()


class TestPortfolioSnapshot(unittest.TestCase):

    def setUp(self):
        _reset()
        import state
        state.init_db()

    @patch('executor.get_eth_balance', return_value=0.05)
    @patch('executor.get_eth_usd_price', return_value=3000.0)
    @patch('executor.get_token_balance', return_value=20.0)
    def test_wallet_only_no_positions(self, mock_bal, mock_price, mock_eth):
        import portfolio_tracker
        result = portfolio_tracker.snapshot()
        # wallet_eth_usd = 0.05 * 3000 = 150.0, usdc = 20.0
        self.assertAlmostEqual(result['wallet_usd'], 170.0, places=1)
        self.assertAlmostEqual(result['position_usd'], 0.0, places=1)
        self.assertAlmostEqual(result['total_usd'], 170.0, places=1)
        self.assertAlmostEqual(result['eth_price'], 3000.0, places=1)

    @patch('executor.get_eth_balance', return_value=0.05)
    @patch('executor.get_eth_usd_price', return_value=3000.0)
    @patch('executor.get_token_balance', return_value=0.0)
    def test_positions_add_5_each(self, mock_bal, mock_price, mock_eth):
        import state
        # Add 2 lend positions + 1 borrow position
        state.add_position('compound_usdc', 'USDC', 5_000_000, 7, '0x01')
        state.add_position('fluid_usdc',    'USDC', 5_000_000, 7, '0x02')
        state.add_position('cb_usdc_weth',  'USDC', '1234',    7, '0x03')
        import portfolio_tracker
        result = portfolio_tracker.snapshot()
        # 3 active positions × $5 = $15
        self.assertAlmostEqual(result['position_usd'], 15.0, places=1)

    @patch('executor.get_eth_balance', return_value=0.05)
    @patch('executor.get_eth_usd_price', return_value=3000.0)
    @patch('executor.get_token_balance', return_value=10.0)
    def test_snapshot_writes_to_daily_stats(self, mock_bal, mock_price, mock_eth):
        import state, portfolio_tracker
        portfolio_tracker.snapshot()
        rows = state.get_daily_stats(1)
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]['portfolio_usd'], 0)
        self.assertAlmostEqual(rows[0]['eth_price'], 3000.0, places=1)

    @patch('executor.get_eth_balance', side_effect=Exception('RPC down'))
    @patch('executor.get_eth_usd_price', return_value=3000.0)
    @patch('executor.get_token_balance', return_value=0.0)
    def test_snapshot_returns_none_on_rpc_error(self, mock_bal, mock_price, mock_eth):
        import portfolio_tracker
        result = portfolio_tracker.snapshot()
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run tests — verify they FAIL**

```powershell
cd C:\Users\Admin\base-agent
python -m pytest test_portfolio_tracker.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'portfolio_tracker'`

- [ ] **Step 3: Implement portfolio_tracker.py**

Create `C:\Users\Admin\base-agent\portfolio_tracker.py`:

```python
"""
portfolio_tracker.py — Daily USD portfolio snapshot.

Approximation:
  wallet_usd   = ETH balance × ETH/USD + USDC balance
  position_usd = active position count × $5.0 (each position is ~$5 by design)
  total_usd    = wallet_usd + position_usd

Writes result to daily_stats via state.update_daily_portfolio().
Returns dict or None on RPC error.
"""

import logging
import os
import json

from dotenv import load_dotenv
load_dotenv()

import executor
import state

log = logging.getLogger(__name__)

with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as _f:
    _CFG = json.load(_f)

USDC_ADDR = _CFG['tokens']['USDC']['address']
POSITION_USD = 5.0  # each position is ~$5 by design


def snapshot() -> dict | None:
    """
    Compute portfolio USD snapshot and persist to daily_stats.

    Returns dict: {wallet_usd, position_usd, total_usd, eth_price, active_count}
    Returns None on RPC error (non-critical — agent continues).
    """
    try:
        eth_bal   = executor.get_eth_balance()
        eth_price = executor.get_eth_usd_price()
        usdc_bal  = executor.get_token_balance(USDC_ADDR, decimals=6)
    except Exception as e:
        log.warning(f'portfolio_tracker: RPC error — skipping snapshot: {e}')
        return None

    wallet_usd = eth_bal * eth_price + usdc_bal

    active     = state.get_active()
    active_count = len(active)
    position_usd = active_count * POSITION_USD

    total_usd = wallet_usd + position_usd

    state.update_daily_portfolio(total_usd, eth_price)
    log.info(
        f'portfolio snapshot: wallet=${wallet_usd:.2f}  positions=${position_usd:.2f}'
        f'  total=${total_usd:.2f}  eth=${eth_price:.0f}  ({active_count} positions)'
    )
    return {
        'wallet_usd':   wallet_usd,
        'position_usd': position_usd,
        'total_usd':    total_usd,
        'eth_price':    eth_price,
        'active_count': active_count,
    }


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout)])
    state.init_db()
    result = snapshot()
    if result:
        print(f'\nTotal portfolio: ${result["total_usd"]:.2f}')
        print(f'  Wallet:        ${result["wallet_usd"]:.2f}')
        print(f'  Positions:     ${result["position_usd"]:.2f} ({result["active_count"]} active)')
        print(f'  ETH price:     ${result["eth_price"]:.0f}')
    else:
        print('Snapshot failed (RPC error).')
        sys.exit(1)
```

- [ ] **Step 4: Run tests — verify they PASS**

```powershell
cd C:\Users\Admin\base-agent
python -m pytest test_portfolio_tracker.py -v
```

Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```powershell
cd C:\Users\Admin\base-agent
git add portfolio_tracker.py test_portfolio_tracker.py
git commit -m "feat: add portfolio_tracker.py — daily USD snapshot to daily_stats"
```

---

## Task 4: weekly_report.py — Monday summary

**Files:**
- Create: `weekly_report.py`
- Create: `test_weekly_report.py`

**Report format:**
```
====================================
BASE AGENT — WEEKLY REPORT
2026-05-26 to 2026-06-01
====================================
Actions (7 days):
  Lend:   12    Borrow:  8    LP:     5
  Vote:    1    Game:    1    Deploy:  0
  Total:  27

Portfolio:
  Latest:   $252.10    ETH: $3,450
  Avg (7d): $245.50
  vs prev 7d: N/A

====================================
```

- [ ] **Step 1: Write failing tests**

Create `C:\Users\Admin\base-agent\test_weekly_report.py`:

```python
"""Unit tests for weekly_report.py — temp DB, no web3."""
import os, sqlite3, tempfile, unittest
from datetime import date, timedelta

_tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_tmp.close()
os.environ['STATE_DB_PATH'] = _tmp.name


def _reset():
    conn = sqlite3.connect(_tmp.name)
    for t in ('positions', 'daily_stats', 'platform_cooldown'):
        conn.execute(f'DROP TABLE IF EXISTS {t}')
    conn.commit()
    conn.close()


def _seed_days(rows: list[dict]):
    """Insert rows into daily_stats. rows: list of {date, **col: val}."""
    conn = sqlite3.connect(_tmp.name)
    for row in rows:
        d = row['date']
        cols = ', '.join(row.keys())
        vals = ', '.join(['?'] * len(row))
        conn.execute(f'INSERT OR REPLACE INTO daily_stats ({cols}) VALUES ({vals})',
                     list(row.values()))
    conn.commit()
    conn.close()


class TestBuildReport(unittest.TestCase):

    def setUp(self):
        _reset()
        import state
        state.init_db()

    def test_report_contains_totals(self):
        today = date.today()
        _seed_days([
            {'date': (today - timedelta(days=i)).isoformat(),
             'lend_count': 2, 'borrow_count': 1, 'lp_count': 1,
             'vote_count': 0, 'game_count': 0, 'deploy_count': 0,
             'portfolio_usd': 200.0, 'eth_price': 3000.0}
            for i in range(7)
        ])
        import weekly_report
        report = weekly_report.build_report()
        self.assertIn('WEEKLY REPORT', report)
        self.assertIn('14', report)   # lend total = 2×7
        self.assertIn('Lend', report)

    def test_report_no_data(self):
        import weekly_report
        report = weekly_report.build_report()
        self.assertIn('No data', report)

    def test_action_totals_correct(self):
        today = date.today()
        _seed_days([
            {'date': (today - timedelta(days=i)).isoformat(),
             'lend_count': 3, 'borrow_count': 2, 'lp_count': 1,
             'vote_count': 0, 'game_count': 1, 'deploy_count': 0,
             'portfolio_usd': 100.0 + i, 'eth_price': 3000.0}
            for i in range(7)
        ])
        import weekly_report
        totals = weekly_report._action_totals(state_days=7)
        self.assertEqual(totals['lend'], 21)     # 3×7
        self.assertEqual(totals['borrow'], 14)   # 2×7
        self.assertEqual(totals['total'], 49)    # (3+2+1+0+1+0)×7


class TestShouldRunToday(unittest.TestCase):

    def test_monday_returns_true(self):
        import weekly_report
        from datetime import date
        monday = date(2026, 6, 1)  # 2026-06-01 is a Monday
        self.assertTrue(weekly_report.should_run(monday))

    def test_non_monday_returns_false(self):
        import weekly_report
        from datetime import date
        tuesday = date(2026, 6, 2)
        self.assertFalse(weekly_report.should_run(tuesday))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run tests — verify they FAIL**

```powershell
cd C:\Users\Admin\base-agent
python -m pytest test_weekly_report.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'weekly_report'`

- [ ] **Step 3: Implement weekly_report.py**

Create `C:\Users\Admin\base-agent\weekly_report.py`:

```python
"""
weekly_report.py — Weekly summary from daily_stats.

Usage:
    python weekly_report.py          # prints report for current week
    python weekly_report.py --force  # print even if not Monday

Scheduled: agent.py calls run() every Monday after daily_job.
"""

import logging
import sys
from datetime import date, timedelta

import state

log = logging.getLogger(__name__)


def should_run(today: date | None = None) -> bool:
    """Return True if today is Monday (weekday == 0)."""
    d = today or date.today()
    return d.weekday() == 0


def _action_totals(state_days: int = 7) -> dict:
    """Sum action counts across last `state_days` days."""
    rows = state.get_daily_stats(state_days)
    totals = dict(lend=0, borrow=0, lp=0, vote=0, game=0, deploy=0)
    for r in rows:
        for k in totals:
            totals[k] += r.get(f'{k}_count', 0)
    totals['total'] = sum(totals.values())
    return totals


def build_report(days: int = 7) -> str:
    """Build and return the weekly report string."""
    rows = state.get_daily_stats(days)
    if not rows:
        return 'No data for weekly report.'

    today    = date.today()
    week_end = today.isoformat()
    week_start = (today - timedelta(days=days - 1)).isoformat()

    totals = _action_totals(days)

    portfolios = [r['portfolio_usd'] for r in rows if r.get('portfolio_usd', 0) > 0]
    eth_prices = [r['eth_price'] for r in rows if r.get('eth_price', 0) > 0]
    latest_portfolio = portfolios[0] if portfolios else 0.0
    avg_portfolio    = sum(portfolios) / len(portfolios) if portfolios else 0.0
    latest_eth       = eth_prices[0] if eth_prices else 0.0

    # Previous week comparison
    prev_rows = state.get_daily_stats(days * 2)
    prev_rows = [r for r in prev_rows if r['date'] < week_start]
    prev_portfolios = [r['portfolio_usd'] for r in prev_rows if r.get('portfolio_usd', 0) > 0]
    prev_avg = sum(prev_portfolios) / len(prev_portfolios) if prev_portfolios else None

    if prev_avg and prev_avg > 0:
        change_pct = (avg_portfolio - prev_avg) / prev_avg * 100
        vs_prev = f'{change_pct:+.1f}%'
    else:
        vs_prev = 'N/A'

    lines = [
        '====================================',
        'BASE AGENT — WEEKLY REPORT',
        f'{week_start} to {week_end}',
        '====================================',
        f'Actions ({days} days):',
        f'  Lend:   {totals["lend"]:<5}  Borrow: {totals["borrow"]:<5}  LP:     {totals["lp"]}',
        f'  Vote:   {totals["vote"]:<5}  Game:   {totals["game"]:<5}  Deploy: {totals["deploy"]}',
        f'  Total:  {totals["total"]}',
        '',
        'Portfolio:',
        f'  Latest:   ${latest_portfolio:,.2f}    ETH: ${latest_eth:,.0f}',
        f'  Avg (7d): ${avg_portfolio:,.2f}',
        f'  vs prev 7d: {vs_prev}',
        '',
        '====================================',
    ]
    return '\n'.join(lines)


def run(force: bool = False) -> str | None:
    """
    Print weekly report if today is Monday (or force=True).
    Returns report string or None if skipped.
    """
    if not force and not should_run():
        log.info('weekly_report: not Monday — skipping')
        return None
    state.init_db()
    report = build_report()
    log.info('\n' + report)
    return report


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s',
                        handlers=[logging.StreamHandler(sys.stdout)])
    force = '--force' in sys.argv
    result = run(force=force)
    if result:
        print(result)
    else:
        print(f'Not Monday ({date.today().strftime("%A")}) — use --force to run anyway.')
```

- [ ] **Step 4: Fix test import for state in test file**

The test file uses `state_days=7` in `_action_totals` call. The function signature uses `state_days` as parameter name. Verify the test's `_seed_days` helper doesn't need `import state` before `weekly_report` test. Since `os.environ['STATE_DB_PATH']` is set at module level before any import, this is fine.

- [ ] **Step 5: Run tests — verify they PASS**

```powershell
cd C:\Users\Admin\base-agent
python -m pytest test_weekly_report.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 6: Commit**

```powershell
cd C:\Users\Admin\base-agent
git add weekly_report.py test_weekly_report.py
git commit -m "feat: add weekly_report.py — Monday action+portfolio summary from daily_stats"
```

---

## Task 5: Hook portfolio_tracker + weekly_report into agent.py

**Files:**
- Modify: `agent.py`

Add two calls at end of `daily_job()`, both best-effort (no failure propagation).

- [ ] **Step 1: Add imports at top of agent.py**

After existing imports, add:
```python
import portfolio_tracker as _portfolio_tracker
import weekly_report as _weekly_report
```

- [ ] **Step 2: Add calls at end of daily_job()**

At the end of `daily_job()`, just before `log.info('=== daily job done ===')`:

```python
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
```

- [ ] **Step 3: Smoke test import**

```powershell
cd C:\Users\Admin\base-agent
DRY_RUN=true python -c "import agent; print('import OK')"
```

Expected: `import OK`

- [ ] **Step 4: Run all Phase C tests together**

```powershell
cd C:\Users\Admin\base-agent
python -m pytest test_daily_stats.py test_portfolio_tracker.py test_weekly_report.py -v
```

Expected: all tests PASS (count depends on final test count — minimum 18 tests).

- [ ] **Step 5: Commit**

```powershell
cd C:\Users\Admin\base-agent
git add agent.py
git commit -m "feat(agent): run portfolio_tracker + weekly_report at end of daily_job"
```

---

## Self-Review

### Spec coverage

| Requirement | Task |
|---|---|
| `portfolio_tracker.py` — daily USD snapshot | Task 3 |
| daily_stats logging ทุก action (lend/borrow/lp/vote/game/deploy count) | Task 1 + 2 |
| `weekly_report.py` — สรุปทุกวันจันทร์ | Task 4 |
| Hook into agent.py | Task 5 |

All requirements covered.

### Placeholder scan

No TBD, TODO, or "similar to Task N" shortcuts. All code blocks are complete and standalone.

### Type consistency

- `state.log_daily_stat(category: str)` — called with string literals throughout
- `state.get_daily_stats(days: int)` — returns `list[dict]` — used correctly in weekly_report
- `portfolio_tracker.snapshot()` — returns `dict | None` — agent.py wraps in try/except, no assumption on return
- `weekly_report.run()` — returns `str | None` — agent.py wraps in try/except
- `weekly_report._action_totals(state_days=7)` — parameter name matches test call

All consistent.
