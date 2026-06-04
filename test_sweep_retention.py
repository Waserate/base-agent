"""Test sweep_tokens.py retention threshold logic."""
import unittest
from unittest.mock import patch, MagicMock

import sys
sys.modules.setdefault('executor', MagicMock())
sys.modules.setdefault('swap', MagicMock())
sys.modules.setdefault('web3', MagicMock())

import sweep_tokens
import rule_engine

class TestRetentionThresholds(unittest.TestCase):

    def test_usdc_below_threshold_not_swept(self):
        excess = rule_engine.usdc_excess(8_000_000)
        self.assertEqual(excess, 0)

    def test_usdc_above_threshold_excess_swept(self):
        excess = rule_engine.usdc_excess(15_000_000)
        self.assertEqual(excess, 5_000_000)

    def test_weth_below_threshold_not_swept(self):
        excess = rule_engine.weth_excess(3_000_000_000_000_000)
        self.assertEqual(excess, 0)

    def test_weth_above_threshold_excess_swept(self):
        excess = rule_engine.weth_excess(8_000_000_000_000_000)
        self.assertEqual(excess, 3_000_000_000_000_000)

    def test_sweep_amount_usdc(self):
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
        self.assertEqual(sweep_tokens._effective_sweep_amount('AERO',  1_000_000_000_000_000_000), 1_000_000_000_000_000_000)
        self.assertEqual(sweep_tokens._effective_sweep_amount('cbBTC', 68000), 68000)
        self.assertEqual(sweep_tokens._effective_sweep_amount('EURC',  4_000_000), 4_000_000)

class TestSweepOneWethPartial(unittest.TestCase):

    def test_weth_partial_retention_calls_unwrap_weth_not_all(self):
        """Verify _sweep_one for WETH calls unwrap_weth(amount) not unwrap_all_weth()."""
        weth_addr = 'some_addr'
        sweep_amt = 3_000_000_000_000_000  # 0.003 WETH to sweep (0.005 retained)

        with patch.object(sweep_tokens.swap, 'unwrap_weth') as mock_partial, \
             patch.object(sweep_tokens.swap, 'unwrap_all_weth') as mock_all:
            sweep_tokens._sweep_one('WETH', weth_addr, 18, 'unwrap', sweep_amt)
            mock_partial.assert_called_once_with(sweep_amt)
            mock_all.assert_not_called()

    def test_weth_partial_sweep_correct_amount(self):
        """Verify correct amount passed to unwrap_weth for above-threshold balance."""
        weth_addr = 'some_addr'
        # 0.008 WETH balance → sweep_amt = 0.003 WETH (0.005 retained)
        sweep_amt = rule_engine.weth_excess(8_000_000_000_000_000)
        self.assertEqual(sweep_amt, 3_000_000_000_000_000)

        with patch.object(sweep_tokens.swap, 'unwrap_weth') as mock_partial:
            sweep_tokens._sweep_one('WETH', weth_addr, 18, 'unwrap', sweep_amt)
            mock_partial.assert_called_once_with(3_000_000_000_000_000)


if __name__ == '__main__':
    unittest.main()
