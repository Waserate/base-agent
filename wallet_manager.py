import json, os, importlib, sys

WALLETS_FILE = os.path.join(os.path.dirname(__file__), 'wallets.json')


def load_wallets() -> list:
    """Returns wallet list. Empty list if wallets.json missing."""
    try:
        with open(WALLETS_FILE) as f:
            return json.load(f).get('wallets', [])
    except FileNotFoundError:
        return []


def save_wallets(wallets: list, last_active: str = None):
    existing = {}
    try:
        with open(WALLETS_FILE) as f:
            existing = json.load(f)
    except FileNotFoundError:
        pass
    existing['wallets'] = wallets
    if last_active is not None:
        existing['last_active'] = last_active
    with open(WALLETS_FILE, 'w') as f:
        json.dump(existing, f, indent=2)


def get_last_active() -> str:
    """Returns last_active wallet id, or first wallet id, or None."""
    try:
        with open(WALLETS_FILE) as f:
            data = json.load(f)
        last = data.get('last_active')
        wallets = data.get('wallets', [])
        if last and any(w['id'] == last for w in wallets):
            return last
        return wallets[0]['id'] if wallets else None
    except FileNotFoundError:
        return None


def get_wallet(wallet_id: str) -> dict:
    for w in load_wallets():
        if w['id'] == wallet_id:
            return w
    return None


def public_wallet(w: dict) -> dict:
    """Strip private_key before sending to browser."""
    return {k: v for k, v in w.items() if k != 'private_key'}


def switch_context(wallet_id: str) -> tuple:
    """
    Hot-swap executor + state to point at wallet_id.
    Sets os.environ then reloads both modules in-place.
    Returns (ok: bool, error: str|None).
    """
    w = get_wallet(wallet_id)
    if not w:
        return False, f'Wallet {wallet_id!r} not found in wallets.json'

    base_dir = os.path.dirname(__file__)
    os.environ['WALLET_ADDRESS']    = w['address']
    os.environ['WALLET_PRIVATE_KEY'] = w.get('private_key', '')
    os.environ['STATE_DB_PATH']     = os.path.join(base_dir, w.get('state_db', f'state_{wallet_id}.db'))

    for mod_name in ('executor', 'state'):
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])

    # Persist last_active so server restart remembers the selection
    try:
        wallets = load_wallets()
        save_wallets(wallets, last_active=wallet_id)
    except Exception:
        pass  # non-fatal

    return True, None
