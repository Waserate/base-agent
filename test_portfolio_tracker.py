"""Unit tests for portfolio_tracker.py — executor mocked, temp DB."""
import os, sqlite3, tempfile, unittest
from unittest.mock import patch

_tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_tmp.close()
os.environ['STATE_DB_PATH'] = _tmp.name


def _reset():
    import state as _s
    conn = sqlite3.connect(_s.DB_PATH)
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
        state.add_position('compound_usdc', 'USDC', 5_000_000, 7, '0x01')
        state.add_position('fluid_usdc',    'USDC', 5_000_000, 7, '0x02')
        state.add_position('cb_usdc_weth',  'USDC', '1234',    7, '0x03')
        import portfolio_tracker
        result = portfolio_tracker.snapshot()
        # 3 active positions x $5 = $15
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
