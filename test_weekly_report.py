"""Unit tests for weekly_report.py — temp DB, no web3."""
import os, sqlite3, tempfile, unittest
from datetime import date, timedelta

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


def _seed_days(rows: list):
    import state as _s
    conn = sqlite3.connect(_s.DB_PATH)
    for row in rows:
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
        self.assertIn('14', report)  # lend total = 2*7
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
        totals = weekly_report._action_totals(days=7)
        self.assertEqual(totals['lend'], 21)    # 3*7
        self.assertEqual(totals['borrow'], 14)  # 2*7
        self.assertEqual(totals['total'], 49)   # (3+2+1+0+1+0)*7


class TestShouldRunToday(unittest.TestCase):

    def test_monday_returns_true(self):
        import weekly_report
        monday = date(2026, 6, 1)  # 2026-06-01 is a Monday
        self.assertTrue(weekly_report.should_run(monday))

    def test_non_monday_returns_false(self):
        import weekly_report
        tuesday = date(2026, 6, 2)
        self.assertFalse(weekly_report.should_run(tuesday))


if __name__ == '__main__':
    unittest.main()
