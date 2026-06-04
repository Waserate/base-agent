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
        """positions: list of fake rows; health_values: {display_name: float}"""
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
