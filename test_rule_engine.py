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
        self.assertGreater(low, 600)


class TestCountActiveByCategory(unittest.TestCase):
    def test_counts_correctly(self):
        active = [
            _pos(1, 'cb_usdc_weth'),
            _pos(2, 'mw_weth_usdc'),
            _pos(3, 'compound_usdc'),
            _pos(4, 'aero_lp_weth_u'),
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
        active = [_pos(i, 'cb_usdc_weth') for i in range(4)]
        result = self._filter(['fl_eth_usdc'], [], [], active, [])
        self.assertNotIn('fl_eth_usdc', result)

    def test_excludes_at_lp_cap(self):
        active = [_pos(i, 'aero_lp_weth_u') for i in range(5)]
        result = self._filter(['uni_lp_weth_u'], [], [], active, [])
        self.assertNotIn('uni_lp_weth_u', result)

    def test_passes_cooldown_check(self):
        with patch.object(rule_engine._state, 'get_cooldown_days', return_value=0):
            result = rule_engine.filter_candidates(
                ['compound_usdc'], set(), set(), _CFG, [], []
            )
        self.assertNotIn('compound_usdc', result)

    def test_rule11_prefers_uncovered_category(self):
        # Week has only 'lend' covered — LP candidate should sort before second lend
        with patch.object(rule_engine._state, 'get_cooldown_days', return_value=999), \
             patch.object(rule_engine, 'categories_this_week', return_value={'lend'}):
            result = rule_engine.filter_candidates(
                ['compound_usdc', 'aero_lp_weth_u'],
                set(), set(), _CFG, [], []
            )
        # aero_lp (lp category, not in week_cats) should come before compound_usdc (lend)
        self.assertEqual(result[0], 'aero_lp_weth_u')
        self.assertEqual(result[1], 'compound_usdc')

    def test_rule11_no_reorder_when_diversity_met(self):
        # Week already has 3+ categories — order is stable (no reorder)
        week = {'lend', 'borrow', 'lp'}
        with patch.object(rule_engine._state, 'get_cooldown_days', return_value=999), \
             patch.object(rule_engine, 'categories_this_week', return_value=week):
            result = rule_engine.filter_candidates(
                ['compound_usdc', 'fluid_usdc'],
                set(), set(), _CFG, [], []
            )
        # Both are lend — order preserved, no crash
        self.assertEqual(len(result), 2)


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
        self.assertEqual(len(d), 2)

    def test_spread_delays_sum_within_window(self):
        random.seed(0)
        d = rule_engine.pick_spread_delays(3)
        self.assertLessEqual(sum(d), 7200)

    def test_spread_delays_single_action(self):
        d = rule_engine.pick_spread_delays(1)
        self.assertEqual(d, [])


class TestRetentionThresholds(unittest.TestCase):
    def test_usdc_excess_above_threshold(self):
        excess = rule_engine.usdc_excess(15_000_000)
        self.assertEqual(excess, 5_000_000)

    def test_usdc_no_excess_below_threshold(self):
        excess = rule_engine.usdc_excess(8_000_000)
        self.assertEqual(excess, 0)

    def test_usdc_exactly_at_threshold(self):
        excess = rule_engine.usdc_excess(10_000_000)
        self.assertEqual(excess, 0)

    def test_weth_excess_above_threshold(self):
        excess = rule_engine.weth_excess(8_000_000_000_000_000)
        self.assertEqual(excess, 3_000_000_000_000_000)

    def test_weth_no_excess_below_threshold(self):
        excess = rule_engine.weth_excess(3_000_000_000_000_000)
        self.assertEqual(excess, 0)


if __name__ == '__main__':
    unittest.main()
