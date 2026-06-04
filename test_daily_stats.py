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
