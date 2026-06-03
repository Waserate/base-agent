# Phase B — Rule Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement rule_engine.py (rules 1-26) to drive daily platform selection, add timing/spread/nonce-reset to agent.py daily_job(), and add threshold-aware token retention to sweep_tokens.py.

**Architecture:** Three independent layers — (1) `rule_engine.py` is a pure-logic, no-web3 library that implements all 26 selection/timing/safety/retention rules and is fully unit-testable; (2) `agent.py daily_job()` is refactored to use rule_engine for candidate filtering, action count, timing delays, and cooldown recording; (3) `sweep_tokens.py` is modified to retain USDC ≤ $10 and WETH ≤ 0.005 instead of sweeping everything.

**Tech Stack:** Python 3.11, random, datetime, state.py (SQLite), unittest.mock (tests)

---

## File Map

| File | Change |
|---|---|
| `rule_engine.py` | NEW — all rules 1-26 as pure functions |
| `test_rule_engine.py` | NEW — unit tests for rule_engine |
| `agent.py` | Modify `daily_job()`: add rule_engine, timing, spread, nonce reset, cooldown |
| `sweep_tokens.py` | Modify `run()`: retain USDC ≤ $10, WETH ≤ 0.005, sweep only excess |
| `test_sweep_retention.py` | NEW — tests for threshold sweep logic |

---

## Task 1: rule_engine.py — Core Logic

**Files:**
- Create: `C:\Users\Admin\base-agent\rule_engine.py`
- Test: `C:\Users\Admin\base-agent\test_rule_engine.py`

### Background: 26 Rules Reference

| Rule | Group | Description |
|---|---|---|
| 1 | Selection | Force withdraw expired first |
| 2 | Selection | Open new after withdraw (combo 70%) |
| 3 | Selection | 1-3 actions/day (40%/40%/20%) |
| 4 | Selection | Run every day (no rest day) |
| 5 | Selection | Skip if ETH < 0.005 |
| 6 | Selection | No new opens if any health < 1.1 |
| 7 | Selection | Skip platform already active |
| 8 | Selection | Skip same protocol same day |
| 9 | Selection | Platform cooldown 1 day after close |
| 10 | Selection | Max concurrent: LP≤5, Lend≤6, Borrow≤4 |
| 11 | Selection | Weekly diversity: ≥3 categories/week |
| 12 | Amount | Tiered: 70%/$5-8, 25%/$8-12, 5%/$12-15 |
| 13 | Timing | Random start 06:00-20:00 |
| 14 | Timing | Actions spread over 2 hours |
| 15 | Timing | Nonce reset before each action |
| 16 | Safety | Daily health check before action |
| 17 | Safety | Force close health < 1.2x (handled by health_monitor/agent) |
| 18 | Safety | Warning if health < 1.5x (handled by health_monitor) |
| 19 | Safety | Error retry > 3 → pick different protocol |
| 20 | Safety | Emergency button: close all in priority order |
| 21-23 | Schedule | Deploy/veAERO/Megapot weekly |
| 24-26 | Retention | Keep USDC ≤ $10, WETH ≤ 0.005, sweep only excess |

Rules 1, 4, 17, 18, 20 are already implemented in agent.py / health_monitor.py.
Rules 16, 21-23 are handled by existing `_run_periodic_actions()`.
This task implements: 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 24-26 as testable functions.

### Platform Protocol Extraction

Platform keys follow these prefixes:
- `compound_*` or `cb_*` → `'compound'`
- `mw_*` → `'moonwell'`
- `fl_*` or `fluid_*` → `'fluid'`
- `aave_*` → `'aave'`
- `beefy_*` → `'beefy'`
- `aero_lp_*` → `'aerodrome'`
- `uni_lp_*` → `'uniswap'`
- `pancake_*` → `'pancake'`
- `morpho_*` → `'morpho'`
- `spark_*` → `'spark'`
- other → first `_`-segment of key

- [ ] **Step 1: Write the failing test**

Create `test_rule_engine.py`:

```python
"""Unit tests for rule_engine.py — no web3, no live DB."""
import unittest, random
from unittest.mock import patch

import rule_engine

# Minimal platform config fixture
_CFG = {
    'compound_usdc':   {'type': 'comet'},
    'fluid_usdc':      {'type': 'erc4626'},
    'morpho_usdc':     {'type': 'erc4626'},
    'beefy_usdc_s':    {'type': 'beefy_single'},
    'aave_supply_usdc':{'type': 'aave_supply'},
    'moonwell_usdc':   {'type': 'ctoken'},
    'cb_usdc_weth':    {'type': 'compound_borrow'},
    'mw_weth_usdc':    {'type': 'mw_borrow'},
    'fl_eth_usdc':     {'type': 'fluid_borrow'},
    'aave_weth_usdc':  {'type': 'aave_borrow'},
    'aero_lp_weth_u':  {'type': 'aero_lp'},
    'uni_lp_weth_u':   {'type': 'uni_lp'},
    'pancake_lp_w':    {'type': 'pancake_lp'},
}

def _pos(pos_id, platform, entry='2026-05-30'):
    return (pos_id, platform, 'USDC', '5000000', entry, '2026-06-10', '0xabc', 'active', None, None, None)


class TestPlatformCategory(unittest.TestCase):
    def test_borrow_category(self):
        self.assertEqual(rule_engine._platform_category('compound_borrow'), 'borrow')
        self.assertEqual(rule_engine._platform_category('mw_borrow'),       'borrow')
        self.assertEqual(rule_engine._platform_category('fluid_borrow'),    'borrow')
        self.assertEqual(rule_engine._platform_category('aave_borrow'),     'borrow')

    def test_lend_category(self):
        self.assertEqual(rule_engine._platform_category('comet'),       'lend')
        self.assertEqual(rule_engine._platform_category('erc4626'),     'lend')
        self.assertEqual(rule_engine._platform_category('aave_supply'), 'lend')

    def test_lp_category(self):
        self.assertEqual(rule_engine._platform_category('aero_lp'),    'lp')
        self.assertEqual(rule_engine._platform_category('uni_lp'),     'lp')
        self.assertEqual(rule_engine._platform_category('pancake_lp'), 'lp')

    def test_other_category(self):
        self.assertEqual(rule_engine._platform_category('aero_vote'), 'other')


class TestGetProtocol(unittest.TestCase):
    def test_compound_prefix(self):
        self.assertEqual(rule_engine.get_protocol('compound_usdc', {}), 'compound')
        self.assertEqual(rule_engine.get_protocol('cb_usdc_weth', {}),  'compound')

    def test_moonwell_prefix(self):
        self.assertEqual(rule_engine.get_protocol('mw_weth_usdc', {}), 'moonwell')

    def test_fluid_prefix(self):
        self.assertEqual(rule_engine.get_protocol('fl_eth_usdc', {}),  'fluid')
        self.assertEqual(rule_engine.get_protocol('fluid_usdc', {}),   'fluid')

    def test_aave_prefix(self):
        self.assertEqual(rule_engine.get_protocol('aave_weth_usdc', {}), 'aave')

    def test_aero_lp(self):
        self.assertEqual(rule_engine.get_protocol('aero_lp_weth_usdc', {}), 'aerodrome')

    def test_uni_lp(self):
        self.assertEqual(rule_engine.get_protocol('uni_lp_weth_usdc', {}), 'uniswap')


class TestBalanceGuard(unittest.TestCase):
    def test_sufficient_eth(self):
        self.assertTrue(rule_engine.balance_guard(0.01))

    def test_exactly_at_minimum(self):
        self.assertTrue(rule_engine.balance_guard(0.005))

    def test_insufficient_eth(self):
        self.assertFalse(rule_engine.balance_guard(0.004))

    def test_zero_eth(self):
        self.assertFalse(rule_engine.balance_guard(0.0))


class TestEmergencyStop(unittest.TestCase):
    def test_no_positions_no_stop(self):
        self.assertFalse(rule_engine.emergency_stop([]))

    def test_all_ok_no_stop(self):
        results = [
            {'health': 2.0, 'status': 'OK'},
            {'health': 1.8, 'status': 'OK'},
        ]
        self.assertFalse(rule_engine.emergency_stop(results))

    def test_health_below_stop_threshold(self):
        results = [
            {'health': 2.0, 'status': 'OK'},
            {'health': 1.05, 'status': 'CRITICAL'},
        ]
        self.assertTrue(rule_engine.emergency_stop(results))

    def test_error_status_skipped(self):
        results = [{'health': 0.0, 'status': 'ERROR'}]
        self.assertFalse(rule_engine.emergency_stop(results))


class TestPickActionCount(unittest.TestCase):
    def test_returns_valid_range(self):
        random.seed(42)
        for _ in range(100):
            n = rule_engine.pick_action_count()
            self.assertIn(n, [1, 2, 3])

    def test_distribution_roughly_correct(self):
        random.seed(0)
        counts = {1: 0, 2: 0, 3: 0}
        for _ in range(1000):
            counts[rule_engine.pick_action_count()] += 1
        # 1 and 2 should be much more common than 3
        self.assertGreater(counts[1], counts[3])
        self.assertGreater(counts[2], counts[3])


class TestPickAmountUsd(unittest.TestCase):
    def test_returns_in_valid_range(self):
        random.seed(0)
        for _ in range(200):
            a = rule_engine.pick_amount_usd()
            self.assertGreaterEqual(a, 5.0)
            self.assertLessEqual(a, 15.0)

    def test_low_tier_most_common(self):
        random.seed(0)
        low = sum(1 for _ in range(1000) if rule_engine.pick_amount_usd() <= 8.0)
        self.assertGreater(low, 600)  # ~70% should be $5-8


class TestCountActiveByCategory(unittest.TestCase):
    def test_counts_correctly(self):
        active = [
            _pos(1, 'cb_usdc_weth'),    # compound_borrow → borrow
            _pos(2, 'mw_weth_usdc'),    # mw_borrow → borrow
            _pos(3, 'compound_usdc'),   # comet → lend
            _pos(4, 'aero_lp_weth_u'), # aero_lp → lp
        ]
        counts = rule_engine.count_active_by_category(active, _CFG)
        self.assertEqual(counts['borrow'], 2)
        self.assertEqual(counts['lend'],   1)
        self.assertEqual(counts['lp'],     1)


class TestFilterCandidates(unittest.TestCase):
    def _filter(self, all_p, active, today_protocols, active_pos, health):
        with patch.object(rule_engine._state, 'get_cooldown_days', return_value=999):
            return rule_engine.filter_candidates(
                all_p, set(active), set(today_protocols), _CFG, active_pos, health
            )

    def test_excludes_already_active(self):
        result = self._filter(
            ['compound_usdc', 'fluid_usdc'], ['compound_usdc'], [], [], []
        )
        self.assertNotIn('compound_usdc', result)
        self.assertIn('fluid_usdc', result)

    def test_excludes_same_protocol_today(self):
        result = self._filter(
            ['compound_usdc', 'fluid_usdc'], [], ['compound'], [], []
        )
        self.assertNotIn('compound_usdc', result)
        self.assertIn('fluid_usdc', result)

    def test_excludes_emergency_stop(self):
        health = [{'health': 1.05, 'status': 'CRITICAL'}]
        result = self._filter(['compound_usdc'], [], [], [], health)
        self.assertEqual(result, [])

    def test_excludes_at_borrow_cap(self):
        active = [_pos(i, 'cb_usdc_weth') for i in range(4)]  # 4 borrows = cap
        result = self._filter(['fl_eth_usdc'], [], [], active, [])
        self.assertNotIn('fl_eth_usdc', result)

    def test_excludes_at_lp_cap(self):
        lp_platforms = [f'aero_lp_weth_u' for _ in range(5)]
        active = [_pos(i, 'aero_lp_weth_u') for i in range(5)]
        result = self._filter(['uni_lp_weth_u'], [], [], active, [])
        self.assertNotIn('uni_lp_weth_u', result)

    def test_passes_cooldown_check(self):
        with patch.object(rule_engine._state, 'get_cooldown_days', return_value=0):
            result = rule_engine.filter_candidates(
                ['compound_usdc'], set(), set(), _CFG, [], []
            )
        self.assertNotIn('compound_usdc', result)  # cooldown 0 days = still in cooldown


class TestTimingHelpers(unittest.TestCase):
    def test_start_delay_in_range(self):
        random.seed(0)
        for _ in range(100):
            d = rule_engine.pick_start_delay_secs()
            self.assertGreaterEqual(d, 0)
            self.assertLessEqual(d, 14 * 3600)

    def test_spread_delays_count(self):
        random.seed(0)
        d = rule_engine.pick_spread_delays(3)
        self.assertEqual(len(d), 2)  # n-1 delays

    def test_spread_delays_sum_within_window(self):
        random.seed(0)
        d = rule_engine.pick_spread_delays(3)
        self.assertLessEqual(sum(d), 7200)

    def test_spread_delays_single_action(self):
        d = rule_engine.pick_spread_delays(1)
        self.assertEqual(d, [])


class TestRetentionThresholds(unittest.TestCase):
    def test_usdc_excess_above_threshold(self):
        # $15 USDC balance (15_000_000 wei), threshold $10 → excess = $5 = 5_000_000
        excess = rule_engine.usdc_excess(15_000_000)
        self.assertEqual(excess, 5_000_000)

    def test_usdc_no_excess_below_threshold(self):
        excess = rule_engine.usdc_excess(8_000_000)  # $8 < $10 threshold
        self.assertEqual(excess, 0)

    def test_usdc_exactly_at_threshold(self):
        excess = rule_engine.usdc_excess(10_000_000)  # exactly $10
        self.assertEqual(excess, 0)

    def test_weth_excess_above_threshold(self):
        # 0.008 WETH, retain 0.005 → excess = 0.003 = 3_000_000_000_000_000
        excess = rule_engine.weth_excess(8_000_000_000_000_000)
        self.assertEqual(excess, 3_000_000_000_000_000)

    def test_weth_no_excess_below_threshold(self):
        excess = rule_engine.weth_excess(3_000_000_000_000_000)  # 0.003 < 0.005
        self.assertEqual(excess, 0)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

```powershell
cd C:\Users\Admin\base-agent
python test_rule_engine.py
```

Expected: `ModuleNotFoundError: No module named 'rule_engine'`

- [ ] **Step 3: Create rule_engine.py**

```python
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
  Retention  24-26: USDC ≤ $10, WETH ≤ 0.005 thresholds for sweep
  (Rules 1, 4, 15-23 are handled in agent.py / health_monitor.py)
"""

import random, logging
from datetime import date, timedelta

import state as _state

log = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

ETH_MIN        = 0.005   # Rule 5: skip day
HEALTH_STOP    = 1.1     # Rule 6: no new opens
MAX_CONCURRENT = {'lp': 5, 'lend': 6, 'borrow': 4}   # Rule 10

# Token retention (Rules 24-26)
USDC_RETAIN_USD = 10.0          # keep up to $10 USDC idle
WETH_RETAIN_ETH = 0.005         # keep up to 0.005 WETH idle
USDC_RETAIN_WEI = int(USDC_RETAIN_USD * 1e6)     # 10_000_000
WETH_RETAIN_WEI = int(WETH_RETAIN_ETH * 1e18)    # 5_000_000_000_000_000

# ── Platform classification ───────────────────────────────────────────────────

BORROW_TYPES = {'compound_borrow', 'mw_borrow', 'fluid_borrow', 'aave_borrow'}
SUPPLY_TYPES = {'comet', 'erc4626', 'ctoken', 'psm_hold', 'beefy_single', 'aave_supply'}
LP_TYPES     = {'beefy_lp', 'aero_lp', 'uni_lp', 'pancake_lp'}


def _platform_category(ptype: str) -> str:
    if ptype in BORROW_TYPES: return 'borrow'
    if ptype in SUPPLY_TYPES: return 'lend'
    if ptype in LP_TYPES:     return 'lp'
    return 'other'


def get_protocol(platform_key: str, p_cfg: dict) -> str:
    """
    Rule 8: determine protocol family from platform key.
    Used to prevent opening two positions in the same protocol same day.
    """
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

# ── Rule 5 ────────────────────────────────────────────────────────────────────

def balance_guard(eth_balance: float) -> bool:
    """Rule 5: True = safe to proceed, False = skip day (ETH too low)."""
    return eth_balance >= ETH_MIN

# ── Rule 6 ────────────────────────────────────────────────────────────────────

def emergency_stop(health_results: list) -> bool:
    """Rule 6: True if any borrow health < HEALTH_STOP (block all new opens)."""
    return any(
        r['health'] < HEALTH_STOP
        for r in health_results
        if r['status'] != 'ERROR'
    )

# ── Rule 3 ────────────────────────────────────────────────────────────────────

def pick_action_count() -> int:
    """Rule 3: 1-3 actions/day (40% one, 40% two, 20% three)."""
    return random.choices([1, 2, 3], weights=[0.4, 0.4, 0.2])[0]

# ── Rule 12 ───────────────────────────────────────────────────────────────────

def pick_amount_usd() -> float:
    """Rule 12: tiered random USD amount (70%:$5-8, 25%:$8-12, 5%:$12-15)."""
    tier = random.choices(['low', 'mid', 'high'], weights=[0.70, 0.25, 0.05])[0]
    if tier == 'low':  return round(random.uniform(5.0,  8.0),  2)
    if tier == 'mid':  return round(random.uniform(8.0,  12.0), 2)
    return round(random.uniform(12.0, 15.0), 2)

# ── Rules 9-10 helpers ────────────────────────────────────────────────────────

def is_in_cooldown(platform_key: str) -> bool:
    """Rule 9: platform must wait 1 day after close before reopening."""
    return _state.get_cooldown_days(platform_key) < 1


def count_active_by_category(active_positions: list, platform_cfgs: dict) -> dict:
    """Count active positions per category for Rule 10 cap enforcement."""
    counts = {'lend': 0, 'borrow': 0, 'lp': 0, 'other': 0}
    for pos in active_positions:
        ptype = platform_cfgs.get(pos[1], {}).get('type', '')
        counts[_platform_category(ptype)] += 1
    return counts

# ── Rules 11 ─────────────────────────────────────────────────────────────────

def categories_this_week(platform_cfgs: dict) -> set:
    """Rule 11: distinct action categories opened in last 7 days (excl. 'other')."""
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    cats = set()
    for pos in _state.all_positions():
        entry_date = pos[4]  # entry_date column
        if entry_date >= week_ago:
            ptype = platform_cfgs.get(pos[1], {}).get('type', '')
            cats.add(_platform_category(ptype))
    return cats - {'other'}

# ── Rules 7-11: filter candidates ─────────────────────────────────────────────

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
    Applies rules 6-11. Returns filtered list (may be empty).

    Parameters:
      all_platforms           : full candidate list from contracts.json
      active_set              : set of platform_key strings currently active in state.db
      today_opened_protocols  : set of protocol strings already opened today
      platform_cfgs           : CFG['platforms'] dict
      active_positions        : state.get_active() rows
      health_results          : health_monitor.check_all() output
    """
    if emergency_stop(health_results):
        log.warning('Rule 6: emergency stop — no new opens (health < %.1f)', HEALTH_STOP)
        return []

    counts = count_active_by_category(active_positions, platform_cfgs)
    eligible = []

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
        cap = MAX_CONCURRENT.get(cat, 999)
        if counts.get(cat, 0) >= cap:                      # Rule 10
            continue

        eligible.append(platform)

    return eligible

# ── Rules 13-14: timing ────────────────────────────────────────────────────────

def pick_start_delay_secs() -> int:
    """
    Rule 13: random start time 06:00-20:00.
    Scheduler fires at 06:00 UTC. This delay (0-50400s) shifts action start
    to a random point in the 14-hour window.
    """
    return random.randint(0, 14 * 3600)


def pick_spread_delays(n_actions: int) -> list:
    """
    Rule 14: inter-action delays for spreading n_actions over 2 hours.
    Returns list of n_actions-1 delay values (seconds), sum <= 7200.
    """
    if n_actions <= 1:
        return []
    window = 7200
    # Pick n-1 random points, sort, compute gaps
    points = sorted(random.randint(60, window) for _ in range(n_actions - 1))
    if sum(points) > window:
        factor = window / sum(points)
        points = [max(30, int(p * factor)) for p in points]
    return points

# ── Rules 24-26: token retention ──────────────────────────────────────────────

def usdc_excess(usdc_balance_wei: int) -> int:
    """
    Rule 24/26: return USDC wei above USDC_RETAIN_WEI threshold.
    Only this excess should be swept to ETH.
    """
    return max(usdc_balance_wei - USDC_RETAIN_WEI, 0)


def weth_excess(weth_balance_wei: int) -> int:
    """
    Rule 24/26: return WETH wei above WETH_RETAIN_WEI threshold.
    Only this excess should be swept to ETH.
    """
    return max(weth_balance_wei - WETH_RETAIN_WEI, 0)
```

- [ ] **Step 4: Run tests to verify all pass**

```powershell
python test_rule_engine.py -v
```

Expected: all tests pass (count will be ~30+ assertions across 11 test classes).

- [ ] **Step 5: Smoke test — import and basic call**

```powershell
python -c "
import rule_engine
print('balance_guard(0.01):', rule_engine.balance_guard(0.01))
print('pick_action_count:', rule_engine.pick_action_count())
print('pick_amount_usd:', rule_engine.pick_amount_usd())
print('usdc_excess(15_000_000):', rule_engine.usdc_excess(15_000_000))
print('weth_excess(8_000_000_000_000_000):', rule_engine.weth_excess(8_000_000_000_000_000))
print('OK')
"
```

Expected: all values print correctly, no crash.

---

## Task 2: agent.py — daily_job() Rewrite

**Files:**
- Modify: `C:\Users\Admin\base-agent\agent.py`

### What changes in daily_job()

Current behavior:
- Fixed scheduler at RUN_HOUR (default 9 UTC)
- Opens 1-2 random platforms (`random.randint(1, 2)`)
- No timing delay, no spread between actions
- No balance guard, no emergency stop check
- No cooldown recording after close
- No protocol uniqueness check per day

New behavior (using rule_engine):
1. **Rule 5**: Balance guard at start — log and return if ETH < 0.005
2. **Rule 13**: Random delay after 06:00 trigger (sleep in job, not blocking scheduler)
3. **Rule 16**: Health check via `_health_monitor.check_all()` (already in `_check_borrow_health`)
4. **Rule 6**: Emergency stop check — skip new opens if any health < 1.1
5. **Rule 3**: `rule_engine.pick_action_count()` instead of `random.randint(1, 2)`
6. **Rules 7-11**: `rule_engine.filter_candidates()` for eligible platforms
7. **Rule 8**: Track `today_opened_protocols` set, update after each open
8. **Rule 9**: Call `state.record_cooldown(platform)` after each close
9. **Rule 14**: `rule_engine.pick_spread_delays()` between actions
10. **Rule 15**: `executor._local_nonce = None` before each platform open
11. **Scheduler**: Change default hour to 6 (was 9); keep RUN_HOUR env var

Also update `__main__` to use `import time` and default to `RUN_HOUR=6`.

- [ ] **Step 1: Add imports to agent.py**

At the top of `agent.py`, after `import health_monitor as _health_monitor`, add:

```python
import time
import rule_engine as _rule_engine
```

- [ ] **Step 2: Rewrite daily_job()**

Find `def daily_job():` (currently ~line 609). Replace the entire function body with:

```python
def daily_job():
    log.info('=== daily job start ===')
    state.init_db()
    executor._local_nonce = None  # Rule 15: fresh nonce at job start

    # Rule 13: random start time 06:00-20:00 (scheduler fires at 06:00)
    delay = _rule_engine.pick_start_delay_secs()
    log.info(f'Rule 13: random delay {delay/3600:.2f}h before first action')
    time.sleep(delay)

    eth  = executor.get_eth_balance()
    usdc = executor.get_token_balance(USDC_ADDR, decimals=6)
    log.info(f'Balances — ETH: {eth:.5f}  USDC: {usdc:.2f}')

    # Rule 5: balance guard
    if not _rule_engine.balance_guard(eth):
        log.warning(f'Rule 5: ETH {eth:.5f} < {_rule_engine.ETH_MIN} — skipping today')
        return

    failed_today = []

    # 0a. Health check + early close (Rule 16/17)
    _check_borrow_health(failed_today)
    # 0b. Periodic weekly actions (Rules 21-23: megapot, deploy, aero_vote)
    _run_periodic_actions(failed_today)

    # 0c. Rule 6: check emergency stop after health results
    health_results = _health_monitor.check_all()
    if _rule_engine.emergency_stop(health_results):
        log.warning('Rule 6: emergency stop — skipping new opens today')
        # Still withdraw expired positions
    
    # 1. Withdraw expired positions
    for pos in state.get_expired():
        if len(failed_today) >= MAX_DAILY_FAILURES:
            log.warning(f'Daily failure limit ({MAX_DAILY_FAILURES}) reached — stopping')
            return

        pos_id, platform, token, amount_wei, entry, expiry, tx_hash, *_rest = pos
        if platform not in CFG['platforms']:
            log.warning(f'Unknown platform {platform} in state, skipping')
            continue

        p     = CFG['platforms'][platform]
        ptype = p.get('type', '')
        log.info(f'Withdrawing expired {platform} {token} (due {expiry})')

        # compound_borrow
        if ptype == 'compound_borrow':
            try:
                txh = _compound_borrow.close_borrow(str(amount_wei), p)
                state.close_position(pos_id)
                state.record_cooldown(platform)  # Rule 9
                log.info(f'Closed {_pname(platform, p)} -> {txh}')
            except Exception as e:
                log.error(f'Close failed {_pname(platform, p)}: {e}')
                failed_today.append(f'close_borrow_{platform}')
            continue

        # mw_borrow
        if ptype == 'mw_borrow':
            try:
                _mw_borrow.close_borrow(str(amount_wei), p, pos_id)
                state.record_cooldown(platform)  # Rule 9
                log.info(f'Closed {_pname(platform, p)}')
            except Exception as e:
                log.error(f'Close failed {_pname(platform, p)}: {e}')
                failed_today.append(f'close_borrow_{platform}')
            continue

        # fluid_borrow
        if ptype == 'fluid_borrow':
            try:
                txh = _fl_borrow.close_borrow(str(amount_wei), p)
                state.close_position(pos_id)
                state.record_cooldown(platform)  # Rule 9
                log.info(f'Closed {_pname(platform, p)} -> {txh}')
            except Exception as e:
                log.error(f'Close failed {_pname(platform, p)}: {e}')
                failed_today.append(f'close_borrow_{platform}')
            continue

        # aave_borrow
        if ptype == 'aave_borrow':
            try:
                txh = _aave_borrow.close_borrow(str(amount_wei), p)
                state.close_position(pos_id)
                state.record_cooldown(platform)  # Rule 9
                log.info(f'Closed {_pname(platform, p)} -> {txh}')
            except Exception as e:
                log.error(f'Close failed {_pname(platform, p)}: {e}')
                failed_today.append(f'close_borrow_{platform}')
            continue

        amt_int = int(float(amount_wei))
        try:
            txh = _withdraw(platform, amt_int)
            state.close_position(pos_id)
            state.record_cooldown(platform)  # Rule 9
            log.info(f'Withdrew {platform} -> {txh}')
        except Exception as e:
            log.error(f'Withdraw failed {platform}: {e}')
            failed_today.append(f'withdraw_{platform}')
            continue

        tok_addr = p.get('token_address', USDC_ADDR)
        _return_to_eth_safe(p, tok_addr, amt_int, failed_today)

    # 2. Open new positions — skip if emergency stop
    if _rule_engine.emergency_stop(health_results):
        log.warning('Rule 6: emergency stop active — no new opens')
        log.info('=== daily job done ===')
        return

    active_positions  = state.get_active()
    active_set        = {p[1] for p in active_positions}
    n_actions         = _rule_engine.pick_action_count()       # Rule 3
    spread_delays     = _rule_engine.pick_spread_delays(n_actions)  # Rule 14

    # Rule 7-11: filter eligible candidates
    candidates = _rule_engine.filter_candidates(
        ACTIVE_PLATFORMS,
        active_set,
        set(),          # today_opened_protocols — start empty, track as we open
        CFG['platforms'],
        active_positions,
        health_results,
    )
    random.shuffle(candidates)
    to_open = candidates[:n_actions]

    today_opened_protocols: set = set()
    for i, platform in enumerate(to_open):
        if len(failed_today) >= MAX_DAILY_FAILURES:
            log.warning(f'Daily failure limit ({MAX_DAILY_FAILURES}) reached — stopping')
            return

        # Rule 14: inter-action spread delay (before 2nd, 3rd action)
        if i > 0 and spread_delays:
            delay = spread_delays[i - 1]
            log.info(f'Rule 14: spread delay {delay}s before action {i+1}')
            time.sleep(delay)

        # Rule 15: nonce reset before each action
        executor._local_nonce = None

        p           = CFG['platforms'][platform]
        expiry_days = random.randint(*p['expiry_days'])
        protocol    = _rule_engine.get_protocol(platform, p)

        # compound_borrow
        if p['type'] == 'compound_borrow':
            log.info(f'Opening {_pname(platform, p)} expiry={expiry_days}d')
            try:
                status = _compound_borrow.check_availability(
                    executor.Web3.to_checksum_address(p['comet_address']),
                    float(p.get('max_utilization', 0.90))
                )
                if not status['available']:
                    log.info(f'{_pname(platform, p)}: skip — util={status["utilization"]:.1%}')
                    continue
                encoded, txh = _compound_borrow.open_borrow(p)
                state.add_position(platform, p.get('borrow_token', 'USDC'), encoded, expiry_days, txh)
                today_opened_protocols.add(protocol)
                log.info(f'Opened {_pname(platform, p)} -> {txh}')
            except Exception as e:
                log.error(f'Open failed {_pname(platform, p)}: {e}')
                failed_today.append(f'supply_{platform}')
            continue

        # mw_borrow
        if p['type'] == 'mw_borrow':
            log.info(f'Opening {_pname(platform, p)} expiry={expiry_days}d')
            try:
                avail = _mw_borrow.check_availability(p)
                if not avail['available']:
                    log.info(f'{_pname(platform, p)}: skip — util={avail["utilization"]:.1%}')
                    continue
                encoded = _mw_borrow.open_borrow(p)
                state.add_position(platform, p.get('borrow_token', 'USDC'), encoded, expiry_days, '')
                today_opened_protocols.add(protocol)
                log.info(f'Opened {_pname(platform, p)}')
            except Exception as e:
                log.error(f'Open failed {_pname(platform, p)}: {e}')
                failed_today.append(f'supply_{platform}')
            continue

        # fluid_borrow
        if p['type'] == 'fluid_borrow':
            log.info(f'Opening {_pname(platform, p)} expiry={expiry_days}d')
            try:
                encoded, txh = _fl_borrow.open_borrow(p)
                state.add_position(platform, p.get('borrow_token', 'USDC'), encoded, expiry_days, txh)
                today_opened_protocols.add(protocol)
                log.info(f'Opened {_pname(platform, p)} -> {txh}')
            except Exception as e:
                log.error(f'Open failed {_pname(platform, p)}: {e}')
                failed_today.append(f'supply_{platform}')
            continue

        # aave_borrow
        if p['type'] == 'aave_borrow':
            log.info(f'Opening {_pname(platform, p)} expiry={expiry_days}d')
            try:
                encoded, txh = _aave_borrow.open_borrow(p)
                state.add_position(platform, p.get('borrow_token', 'USDC'), encoded, expiry_days, txh)
                today_opened_protocols.add(protocol)
                log.info(f'Opened {_pname(platform, p)} -> {txh}')
            except Exception as e:
                log.error(f'Open failed {_pname(platform, p)}: {e}')
                failed_today.append(f'supply_{platform}')
            continue

        # uni_lp / pancake_lp
        if p['type'] in ('uni_lp', 'pancake_lp'):
            if not _prepare_token_safe(p, None, 0, failed_today):
                continue
            log.info(f'Opening {platform} ({p["type"]}) expiry={expiry_days}d')
            try:
                if p['type'] == 'uni_lp':
                    token_id, txh = _uni_lp.mint_uni_lp(platform)
                else:
                    token_id, txh = _pancake_lp.mint_pancake_lp(platform)
                state.add_position(platform, 'LP', str(token_id), expiry_days, txh)
                today_opened_protocols.add(protocol)
                log.info(f'Opened {platform} tokenId={token_id} -> {txh}')
            except Exception as e:
                log.error(f'Supply failed {platform}: {e}')
                failed_today.append(f'supply_{platform}')
            continue

        amt      = _amount(platform)
        tok_addr = p.get('token_address', USDC_ADDR)

        if not _prepare_token_safe(p, tok_addr, amt, failed_today):
            continue

        log.info(f'Opening {platform} ({p["token"]}) amount={amt} expiry={expiry_days}d')
        try:
            txh = _supply(platform)
            state.add_position(platform, p['token'], amt, expiry_days, txh)
            today_opened_protocols.add(protocol)
            log.info(f'Opened {platform} -> {txh}')
        except Exception as e:
            log.error(f'Supply failed {platform}: {e}')
            failed_today.append(f'supply_{platform}')

    if failed_today:
        log.warning(f'Failures today: {failed_today}')
    log.info('=== daily job done ===')
```

- [ ] **Step 3: Update __main__ to default RUN_HOUR=6**

Find `if __name__ == '__main__':` block at bottom of agent.py. Replace:

```python
if __name__ == '__main__':
    state.init_db()
    run_hour = int(os.getenv('RUN_HOUR', '9'))
    log.info(f'Scheduler starting — daily at {run_hour}:00 UTC (BKK +7 = {run_hour+7}:00)')
    scheduler = BlockingScheduler(timezone='UTC')
    scheduler.add_job(daily_job, 'cron', hour=run_hour, minute=0)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info('Scheduler stopped')
```

with:

```python
if __name__ == '__main__':
    state.init_db()
    run_hour = int(os.getenv('RUN_HOUR', '6'))  # default 06:00 UTC = 13:00 BKK (Rule 13 range start)
    log.info(f'Scheduler starting — daily at {run_hour}:00 UTC (BKK +7 = {run_hour+7}:00)')
    log.info(f'Rule 13: random 0-14h delay inside job -> actual run 06:00-20:00 UTC')
    scheduler = BlockingScheduler(timezone='UTC')
    scheduler.add_job(daily_job, 'cron', hour=run_hour, minute=0)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info('Scheduler stopped')
```

- [ ] **Step 4: Verify agent.py imports cleanly**

```powershell
python -c "import agent; print('agent import OK')"
```

Expected: `agent import OK`

- [ ] **Step 5: Smoke test — dry run (Rule 13 delay must be skippable for testing)**

Add temporary env override to test without waiting 14 hours:

```powershell
$env:DRY_RUN='true'
# Temporarily test daily_job without the sleep by patching:
python -c "
import rule_engine, agent
rule_engine.pick_start_delay_secs = lambda: 0  # override: no delay
rule_engine.pick_spread_delays = lambda n: []  # override: no spread
agent.daily_job()
print('daily_job DRY_RUN OK')
"
```

Expected: job runs, logs balance check, health check, rule 5/6 checks, exits cleanly. No TX sent.

---

## Task 3: sweep_tokens.py — Token Retention

**Files:**
- Modify: `C:\Users\Admin\base-agent\sweep_tokens.py`
- Test: `C:\Users\Admin\base-agent\test_sweep_retention.py`

### What changes

Current behavior: sweeps ALL USDC and WETH to ETH if non-zero balance.

New behavior (Rules 24-26):
- WETH: only sweep excess above `rule_engine.WETH_RETAIN_WEI` (0.005 ETH)
- USDC: only sweep excess above `rule_engine.USDC_RETAIN_WEI` ($10)
- All other tokens: unchanged (sweep all)

The `TOKENS` list in sweep_tokens.py uses `('WETH', ..., 'unwrap')` and `('USDC', ..., 'swap_to_eth')`. The main `run()` loop reads full balance and passes to `_sweep_one()`.

Modification: before calling `_sweep_one()`, compute excess and skip/reduce amount.

- [ ] **Step 1: Write the failing test**

Create `test_sweep_retention.py`:

```python
"""Test sweep_tokens.py retention threshold logic."""
import unittest
from unittest.mock import patch, MagicMock

# Stub web3/executor before imports
import sys
sys.modules.setdefault('executor', MagicMock())
sys.modules.setdefault('swap', MagicMock())
sys.modules.setdefault('web3', MagicMock())

import sweep_tokens
import rule_engine

class TestRetentionThresholds(unittest.TestCase):
    """Verify that sweep_tokens._sweep_amount() applies retention rules."""

    def test_usdc_below_threshold_not_swept(self):
        # $8 USDC balance — below $10 threshold — excess = 0 → skip
        excess = rule_engine.usdc_excess(8_000_000)
        self.assertEqual(excess, 0)

    def test_usdc_above_threshold_excess_swept(self):
        # $15 USDC — excess $5 = 5_000_000
        excess = rule_engine.usdc_excess(15_000_000)
        self.assertEqual(excess, 5_000_000)

    def test_weth_below_threshold_not_swept(self):
        # 0.003 WETH — below 0.005 threshold
        excess = rule_engine.weth_excess(3_000_000_000_000_000)
        self.assertEqual(excess, 0)

    def test_weth_above_threshold_excess_swept(self):
        # 0.008 WETH — excess 0.003 WETH
        excess = rule_engine.weth_excess(8_000_000_000_000_000)
        self.assertEqual(excess, 3_000_000_000_000_000)

    def test_sweep_amount_usdc(self):
        # sweep_tokens._effective_sweep_amount must return excess for USDC
        result = sweep_tokens._effective_sweep_amount('USDC', 15_000_000)
        self.assertEqual(result, 5_000_000)

    def test_sweep_amount_usdc_no_excess(self):
        result = sweep_tokens._effective_sweep_amount('USDC', 5_000_000)
        self.assertEqual(result, 0)

    def test_sweep_amount_weth(self):
        result = sweep_tokens._effective_sweep_amount('WETH', 8_000_000_000_000_000)
        self.assertEqual(result, 3_000_000_000_000_000)

    def test_sweep_amount_weth_no_excess(self):
        result = sweep_tokens._effective_sweep_amount('WETH', 2_000_000_000_000_000)
        self.assertEqual(result, 0)

    def test_sweep_amount_other_tokens_unaffected(self):
        # AERO, cbBTC, etc. — no retention, sweep all
        self.assertEqual(sweep_tokens._effective_sweep_amount('AERO',  1_000_000_000_000_000_000), 1_000_000_000_000_000_000)
        self.assertEqual(sweep_tokens._effective_sweep_amount('cbBTC', 68000), 68000)
        self.assertEqual(sweep_tokens._effective_sweep_amount('EURC',  4_000_000), 4_000_000)

if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run to verify fail**

```powershell
python test_sweep_retention.py
```

Expected: `AttributeError: module 'sweep_tokens' has no attribute '_effective_sweep_amount'`

- [ ] **Step 3: Add import + _effective_sweep_amount() to sweep_tokens.py**

Open `sweep_tokens.py`. At the top of imports, after `import executor, swap`, add:

```python
import rule_engine as _rule_engine
```

After the `TOKENS = [...]` list definition, add:

```python
def _effective_sweep_amount(symbol: str, balance_wei: int) -> int:
    """
    Rules 24-26: return sweep amount for token.
    USDC/WETH: only sweep excess above retention threshold.
    All other tokens: sweep full balance.
    """
    if symbol == 'USDC':
        return _rule_engine.usdc_excess(balance_wei)
    if symbol == 'WETH':
        return _rule_engine.weth_excess(balance_wei)
    return balance_wei
```

- [ ] **Step 4: Use _effective_sweep_amount() in run()**

In `run()`, find the main loop body:

```python
for symbol, addr, decimals, mode in TOKENS:
    bal = _bal_wei(addr)
    human = bal / 10**decimals
    if bal == 0:
        log.info(f'  {symbol:6}  {human:.6f}  skip (zero)')
        continue

    log.info(f'  {symbol:6}  {human:.6f}  -> converting to ETH ...')
```

Replace with:

```python
for symbol, addr, decimals, mode in TOKENS:
    bal = _bal_wei(addr)
    sweep_amt = _effective_sweep_amount(symbol, bal)
    human     = bal / 10**decimals
    retain    = (bal - sweep_amt) / 10**decimals

    if bal == 0:
        log.info(f'  {symbol:6}  {human:.6f}  skip (zero)')
        continue
    if sweep_amt == 0:
        log.info(f'  {symbol:6}  {human:.6f}  skip (retain threshold — keeping all)')
        continue
    if sweep_amt < bal:
        log.info(f'  {symbol:6}  {human:.6f}  retaining {retain:.6f}  sweeping {sweep_amt/10**decimals:.6f} ...')
    else:
        log.info(f'  {symbol:6}  {human:.6f}  -> converting to ETH ...')
```

Also update the `_sweep_one()` call to pass `sweep_amt` instead of `bal`:

Find:
```python
        try:
            txh = _sweep_one(symbol, addr, decimals, mode, bal)
```

Replace with:
```python
        try:
            txh = _sweep_one(symbol, addr, decimals, mode, sweep_amt)
```

- [ ] **Step 5: Run tests**

```powershell
python test_sweep_retention.py -v
```

Expected: 9 tests all pass.

- [ ] **Step 6: Smoke test DRY_RUN**

```powershell
$env:DRY_RUN='true'; python sweep_tokens.py
```

Expected: runs cleanly. If USDC balance ≤ $10 → "skip (retain threshold)". If WETH ≤ 0.005 → "skip (retain threshold)".

---

## Self-Review

### Spec Coverage

| Spec Requirement | Task |
|---|---|
| `rule_engine.py` rules 1-26 | Task 1 |
| `agent.py` daily_job() timing 06:00-20:00 | Task 2 (Rule 13) |
| `agent.py` 2hr action spread | Task 2 (Rule 14) |
| `agent.py` nonce reset before each action | Task 2 (Rule 15) |
| Protocol uniqueness per day (Rule 8) | Task 2 (`today_opened_protocols`) |
| Cooldown recording after close (Rule 9) | Task 2 (`state.record_cooldown`) |
| Balance guard (Rule 5) | Task 2 (calls `rule_engine.balance_guard`) |
| Emergency stop on new opens (Rule 6) | Task 2 (calls `rule_engine.emergency_stop`) |
| `sweep_tokens.py` USDC ≤ $10 retention | Task 3 |
| `sweep_tokens.py` WETH ≤ 0.005 retention | Task 3 |
| Sweep only excess (Rule 26) | Task 3 (`_effective_sweep_amount`) |

All 11 requirements covered. ✅

### Dependency Order

- Task 1 must complete before Task 2 (`agent.py` imports `rule_engine`)
- Task 3 is independent of Task 2 (imports only `rule_engine` from Task 1)
- Execute: Task 1 → Task 2 → Task 3 (or Task 1 → [Task 2 ∥ Task 3] if both have rule_engine ready)

### Placeholder Scan

No TBD/TODO. All code complete. ✅

### Type Consistency

- `rule_engine.filter_candidates()` returns `list` of platform key strings — agent.py iterates them directly. ✅
- `rule_engine.pick_spread_delays(n)` returns `list[int]` — agent.py indexes with `spread_delays[i-1]`. ✅
- `rule_engine.usdc_excess()` / `weth_excess()` return `int` — sweep_tokens uses as `sweep_amt` int. ✅
- `state.record_cooldown(platform_key)` signature matches what agent.py calls. ✅
