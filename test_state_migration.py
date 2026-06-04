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
