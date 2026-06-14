import json, os, importlib, sys, re

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
    os.environ['WALLET_ID']          = wallet_id
    os.environ['WALLET_ADDRESS']     = w['address']
    os.environ['WALLET_PRIVATE_KEY'] = w.get('private_key', '')
    os.environ['STATE_DB_PATH']      = os.path.join(base_dir, w.get('state_db', f'state_{wallet_id}.db'))

    # Reload executor + state FIRST (they read the new env), then every module
    # that captured executor.WALLET / wallet-bound state at import time. Order
    # matters: dependents re-run `WALLET = executor.WALLET` against the freshly
    # reloaded executor. Skipping this caused swap recipients to stay on the
    # previously-active wallet — ifond paid while test received the tokens.
    # (swap.py now reads executor.WALLET live, but reloading is kept as defense
    # in depth for withdraw_all/sweep_tokens/onchain_recovery balance reads.)
    # executor + state read the new env directly. swap/sweep/withdraw/recovery
    # capture executor.WALLET. megapot/deploy hold their OWN frozen PRIVATE_KEY +
    # WALLET + recipient from env — reload re-reads them for the switched wallet.
    for mod_name in ('executor', 'state', 'swap', 'sweep_tokens',
                     'withdraw_all', 'onchain_recovery',
                     'megapot', 'megapot_claim', 'deploy_contract'):
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])

    # Persist last_active so server restart remembers the selection
    try:
        wallets = load_wallets()
        save_wallets(wallets, last_active=wallet_id)
    except Exception:
        pass  # non-fatal

    return True, None


def get_id_for_address(address: str) -> str | None:
    """Find wallet id matching address (case-insensitive). Returns None if not found."""
    addr_lower = address.lower()
    for w in load_wallets():
        if w['address'].lower() == addr_lower:
            return w['id']
    return None


def _slugify(name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    return slug or 'wallet'


def add_wallet(name: str, address: str, private_key: str, delete_pin: str = '') -> tuple:
    """
    Validate + append new wallet to wallets.json.
    Returns (entry: dict, error: str|None).
    """
    from web3 import Web3

    name        = (name or '').strip()
    address     = (address or '').strip()
    private_key = (private_key or '').strip()
    delete_pin  = (delete_pin or '').strip()

    if not name:
        return None, 'Name required'
    if not address:
        return None, 'Address required'
    if not delete_pin:
        return None, 'Delete PIN required'
    if not Web3.is_address(address):
        return None, f'Invalid address: {address}'

    address = Web3.to_checksum_address(address)

    existing = load_wallets()

    if any(w['address'].lower() == address.lower() for w in existing):
        return None, 'Address already registered'

    base_id      = _slugify(name)
    existing_ids = {w['id'] for w in existing}
    wid, suffix  = base_id, 2
    while wid in existing_ids:
        wid = f'{base_id}_{suffix}'
        suffix += 1

    entry = {
        'id':          wid,
        'name':        name,
        'address':     address,
        'private_key': private_key,
        'active':      True,
        'state_db':    f'state_{wid}.db',
        'avatar_path': 'natsu_pensive.png',
        'delete_pin':  delete_pin,
    }
    existing.append(entry)
    save_wallets(existing)
    return entry, None


def remove_wallet(wallet_id: str, pin: str) -> tuple:
    """
    Remove wallet from wallets.json after PIN verification.
    Returns (ok: bool, error: str|None).
    State DB file is kept on disk (not deleted).
    """
    try:
        with open(WALLETS_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return False, 'wallets.json not found'

    wallets = data.get('wallets', [])
    target  = next((w for w in wallets if w['id'] == wallet_id), None)
    if target is None:
        return False, f'Wallet {wallet_id!r} not found'

    stored_pin = target.get('delete_pin', '')
    if not stored_pin or pin.strip() != stored_pin:
        return False, 'Wrong PIN'

    data['wallets'] = [w for w in wallets if w['id'] != wallet_id]

    # If last_active pointed to deleted wallet, reset to first remaining
    remaining = data['wallets']
    if data.get('last_active') == wallet_id:
        data['last_active'] = remaining[0]['id'] if remaining else None

    with open(WALLETS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

    return True, None


def toggle_active(wallet_id: str) -> tuple:
    """
    Flip active flag for wallet_id in wallets.json.
    Returns (ok: bool, new_active: bool|None, error: str|None).
    """
    try:
        with open(WALLETS_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return False, None, 'wallets.json not found'

    wallets = data.get('wallets', [])
    target = next((w for w in wallets if w['id'] == wallet_id), None)
    if target is None:
        return False, None, f'Wallet {wallet_id!r} not found'

    target['active'] = not target.get('active', True)
    data['wallets'] = wallets
    with open(WALLETS_FILE, 'w') as f:
        json.dump(data, f, indent=2)

    return True, target['active'], None
