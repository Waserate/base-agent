import sqlite3, os
from datetime import date, timedelta

def _default_db_path():
    """Resolve STATE_DB_PATH. Prefer explicit env; else last_active wallet's DB.
    Legacy state.db is gone — never recreate it. Raise only if no wallet exists."""
    p = os.environ.get('STATE_DB_PATH')
    if p:
        return p
    import wallet_manager as _wm
    wid = _wm.get_last_active()
    w   = _wm.get_wallet(wid) if wid else None
    if w and w.get('state_db'):
        return os.path.join(os.path.dirname(__file__), w['state_db'])
    raise RuntimeError('STATE_DB_PATH unset and no wallet found in wallets.json')

DB_PATH = _default_db_path()

def _conn():
    return sqlite3.connect(DB_PATH)

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


def migrate_db():
    """Add new columns/tables without losing existing data. Idempotent."""
    with _conn() as c:
        # Guard: only add columns if positions table already exists
        table_exists = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='positions'"
        ).fetchone()
        if table_exists:
            for col_def in ('opened_usd REAL', 'closed_usd REAL', 'gas_cost_wei INTEGER'):
                try:
                    c.execute(f'ALTER TABLE positions ADD COLUMN {col_def}')
                except sqlite3.OperationalError:
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

def get_active(platform=None):
    q, p = "SELECT * FROM positions WHERE status='active'", []
    if platform:
        q += ' AND platform=?'; p.append(platform)
    with _conn() as c:
        return c.execute(q, p).fetchall()

def get_expired():
    today = date.today().isoformat()
    with _conn() as c:
        return c.execute(
            "SELECT * FROM positions WHERE status='active' AND expiry_date <= ?", (today,)
        ).fetchall()

def add_position(platform, token, amount_wei, expiry_days, tx_hash):
    today = date.today().isoformat()
    expiry = (date.today() + timedelta(days=expiry_days)).isoformat()
    with _conn() as c:
        c.execute(
            'INSERT INTO positions (platform,token,amount_wei,entry_date,expiry_date,tx_hash) VALUES (?,?,?,?,?,?)',
            (platform, token, str(amount_wei), today, expiry, tx_hash)
        )

def close_position(pos_id):
    """Close position in current DB, then immediately sync to all other known DBs
    by matching platform+entry_date — prevents orphan active rows in legacy DBs."""
    _DIR = os.path.dirname(os.path.abspath(__file__))
    with _conn() as c:
        c.execute("UPDATE positions SET status='closed' WHERE id=?", (pos_id,))
        row = c.execute(
            "SELECT platform, entry_date FROM positions WHERE id=?", (pos_id,)
        ).fetchone()
    if not row:
        return
    platform, entry_date = row
    # Sync closure to all other state DBs (cross-DB dedup)
    import glob as _glob
    for db_path in _glob.glob(os.path.join(_DIR, 'state*.db')):
        if os.path.abspath(db_path) == os.path.abspath(DB_PATH):
            continue
        try:
            with sqlite3.connect(db_path) as cx:
                cx.execute(
                    "UPDATE positions SET status='closed' WHERE platform=? AND entry_date=? AND status='active'",
                    (platform, entry_date)
                )
        except Exception:
            pass

def all_positions():
    with _conn() as c:
        return c.execute("SELECT * FROM positions ORDER BY id DESC").fetchall()

def get_last_entry_date(platform: str) -> str | None:
    """Return ISO entry_date of most recent row for platform, or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT entry_date FROM positions WHERE platform=? ORDER BY id DESC LIMIT 1",
            (platform,)
        ).fetchone()
    return row[0] if row else None


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

def backup_db(backups_dir: str = None) -> str:
    """
    Copy state.db to backups/state_YYYYMMDD.db.
    Keeps last 30 days, deletes older files.
    Returns path of backup file created.
    """
    import shutil
    if backups_dir is None:
        backups_dir = os.path.join(os.path.dirname(DB_PATH), 'backups')
    os.makedirs(backups_dir, exist_ok=True)
    today      = date.today().isoformat()
    dest       = os.path.join(backups_dir, f'state_{today}.db')
    shutil.copy2(DB_PATH, dest)
    # Prune backups older than 30 days
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    for fname in os.listdir(backups_dir):
        if fname.startswith('state_') and fname.endswith('.db'):
            file_date = fname[6:-3]  # state_YYYY-MM-DD.db → YYYY-MM-DD
            if file_date < cutoff:
                try:
                    os.remove(os.path.join(backups_dir, fname))
                except OSError:
                    pass
    return dest


def restore_from_recovery(positions_list: list) -> int:
    """
    Insert recovered on-chain positions into DB.
    positions_list: list of dicts from onchain_recovery.scan()
    Each dict must have: platform, token, amount_wei, entry_date, expiry_date
    Skips platforms already active in DB.
    Returns count of rows inserted.
    """
    active_platforms = {pos[1] for pos in get_active()}
    inserted = 0
    with _conn() as c:
        for p in positions_list:
            if p['platform'] in active_platforms:
                continue
            c.execute(
                'INSERT INTO positions '
                '(platform, token, amount_wei, entry_date, expiry_date, tx_hash, status) '
                'VALUES (?,?,?,?,?,?,?)',
                (p['platform'], p['token'], str(p['amount_wei']),
                 p.get('entry_date', date.today().isoformat()),
                 p['expiry_date'], None, 'active')
            )
            inserted += 1
    return inserted


def latest_backup(backups_dir: str = None) -> str | None:
    """Return path of most recent backup file, or None if none exist."""
    if backups_dir is None:
        backups_dir = os.path.join(os.path.dirname(DB_PATH), 'backups')
    if not os.path.isdir(backups_dir):
        return None
    files = sorted(
        [f for f in os.listdir(backups_dir) if f.startswith('state_') and f.endswith('.db')],
        reverse=True
    )
    return os.path.join(backups_dir, files[0]) if files else None


def restore_from_backup(backup_path: str) -> bool:
    """Overwrite state.db with a backup file. Returns True on success."""
    import shutil
    try:
        shutil.copy2(backup_path, DB_PATH)
        return True
    except Exception:
        return False


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


def update_position_gas(pos_id: int, gas_cost_wei: int):
    """Write gas_cost_wei to a position row."""
    with _conn() as c:
        c.execute('UPDATE positions SET gas_cost_wei=? WHERE id=?', (gas_cost_wei, pos_id))


def update_daily_gas_vol(entry_date: str, gas_usd: float, volume_usd: float):
    """Accumulate gas_usd + volume_usd into daily_stats for a given date."""
    with _conn() as c:
        c.execute(
            'INSERT INTO daily_stats (date, gas_usd, volume_usd) VALUES (?,?,?) '
            'ON CONFLICT(date) DO UPDATE SET '
            'gas_usd = gas_usd + excluded.gas_usd, '
            'volume_usd = volume_usd + excluded.volume_usd',
            (entry_date, gas_usd, volume_usd)
        )


def compute_positions_totals(cutoff_date: str) -> dict:
    """
    Compute gas + volume totals directly from the positions table.
    gas_usd: sum gas_cost_wei / 1e18 * eth_price (fetched once from executor).
    volume_usd: sum opened_usd (fallback $5 per position when NULL).
    Returns {gas_eth, gas_usd, volume_usd, count}.
    """
    with _conn() as c:
        rows = c.execute(
            'SELECT gas_cost_wei, opened_usd FROM positions WHERE entry_date >= ?',
            (cutoff_date,)
        ).fetchall()

    total_gas_wei = sum(int(r[0]) for r in rows if r[0] is not None)
    total_vol     = sum(float(r[1]) if r[1] is not None else 5.0 for r in rows)

    try:
        import executor as _ex
        eth_price = _ex.get_eth_usd_price()
    except Exception:
        eth_price = 2000.0

    gas_eth = total_gas_wei / 1e18
    gas_usd = gas_eth * eth_price

    return {
        'gas_eth':    round(gas_eth, 6),
        'gas_usd':    round(gas_usd, 4),
        'volume_usd': round(total_vol, 2),
        'count':      len(rows),
    }
