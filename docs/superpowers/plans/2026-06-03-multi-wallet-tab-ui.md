# Multi-Wallet Tab UI (Phase MW-1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add wallet-tab UI to the dashboard so the user can switch between "Test" and "Ifond" wallets without restarting the server — each tab shows its own positions, balance, plan, and health from the correct state DB and on-chain address.

**Architecture:** Single-process serve_dashboard.py stores `_active_wallet_id`. Switching tabs POSTs to `/api/wallets/switch`, which hot-swaps `os.environ['WALLET_ADDRESS']` + `os.environ['STATE_DB_PATH']`, reloads `executor` and `state` modules in-place (importlib.reload modifies the module object, so existing references pick up the new values), then clears all caches. All subsequent API calls use the new context. `wallets.json` is the single source of truth; `.env` stays as fallback for agent.py.

**Tech Stack:** Python stdlib (importlib, json, os), web3.py, Python http.server, vanilla JS in dashboard.html.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `wallets.json` | Create | Wallet registry — two entries (test + ifond), private keys stored here, gitignored |
| `wallet_manager.py` | Create | load/save/switch_context — isolates all wallet-switching logic |
| `serve_dashboard.py` | Modify | Add `/api/wallets` GET + `/api/wallets/switch` POST; call switch on startup; clear caches on switch |
| `dashboard.html` | Modify | Wallet tab bar in header; JS fetchWallets + switchWallet |

**Not in scope (MW-2+):** agent.py loop, Stop/Play per wallet, avatar upload, GitHub push.

---

## Task 1: Create wallets.json

**Files:**
- Create: `C:\Users\Admin\base-agent\wallets.json`

- [ ] **Step 1: Read current .env to get test wallet values**

Open `.env`. Note:
- `WALLET_ADDRESS` → test wallet address
- `WALLET_PRIVATE_KEY` → test wallet private key

Ifond wallet address from memory: `0xA391a90cdC4A5f47f25a271C61C8Cf09A55db7eF`
Ifond private key: user must fill in manually (leave blank string for now).

- [ ] **Step 2: Create wallets.json**

```json
{
  "last_active": "test",
  "wallets": [
    {
      "id": "test",
      "name": "Test Wallet",
      "address": "0xfEA3BEfE1971bf895c3FeEb07d3435f6D6a03E6f",
      "private_key": "<paste WALLET_PRIVATE_KEY from .env>",
      "active": true,
      "state_db": "state_test.db",
      "avatar_path": "natsu_pensive.png"
    },
    {
      "id": "ifond",
      "name": "Ifond",
      "address": "0xA391a90cdC4A5f47f25a271C61C8Cf09A55db7eF",
      "private_key": "<fill in ifond private key>",
      "active": false,
      "state_db": "state_ifond.db",
      "avatar_path": "natsu_pensive.png"
    }
  ]
}
```

**Note:** Replace `<paste WALLET_PRIVATE_KEY from .env>` with the actual value from `.env`. Replace ifond private key when ready. Both are plaintext — `wallets.json` must stay gitignored (already handled by MW-3; verify `.gitignore` contains `wallets.json`).

- [ ] **Step 3: Verify .gitignore has wallets.json**

Check `C:\Users\Admin\base-agent\.gitignore`. If `wallets.json` is not in it, add it.

---

## Task 2: Create wallet_manager.py

**Files:**
- Create: `C:\Users\Admin\base-agent\wallet_manager.py`

- [ ] **Step 1: Write wallet_manager.py**

```python
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
```

- [ ] **Step 2: Quick smoke test**

Run in PowerShell from base-agent dir:
```powershell
python -c "import wallet_manager; ws = wallet_manager.load_wallets(); print([w['id'] for w in ws])"
```
Expected output: `['test', 'ifond']`

---

## Task 3: Add wallet API endpoints to serve_dashboard.py

**Files:**
- Modify: `C:\Users\Admin\base-agent\serve_dashboard.py`

### 3a: Import wallet_manager and init on startup

- [ ] **Step 1: Add import at top of serve_dashboard.py**

After `import state` (line ~8), add:

```python
import wallet_manager as _wallet_mgr
```

- [ ] **Step 2: Add cache-clear helper after the cache variable declarations (~line 15)**

Add after `_balance_lock = threading.Lock()`:

```python
def _clear_all_caches():
    """Call after wallet switch to force fresh data for new wallet."""
    global _health_cache, _balance_cache
    with _health_lock:
        _health_cache  = {'ts': 0.0, 'data': None}
    with _balance_lock:
        _balance_cache = {'ts': 0.0, 'data': None}
    _LIVE_USD_CACHE.clear()
```

- [ ] **Step 3: Add wallet-context init in `if __name__ == '__main__':` block**

At the start of the `if __name__ == '__main__':` block (line ~1237), before `state.init_db()`, add:

```python
# Init wallet context from wallets.json (falls back to .env if file missing)
_wid = _wallet_mgr.get_last_active()
if _wid:
    ok, err = _wallet_mgr.switch_context(_wid)
    if not ok:
        print(f'[wallet] Warning: {err} — using .env defaults')
```

### 3b: Add GET /api/wallets

- [ ] **Step 4: Add to do_GET inside the if/elif chain (after `/api/settings` block)**

```python
elif path == '/api/wallets':
    wallets = _wallet_mgr.load_wallets()
    active_id = os.environ.get('WALLET_ADDRESS', '').lower()
    result = []
    for w in wallets:
        pw = _wallet_mgr.public_wallet(w)
        pw['is_active'] = (w['address'].lower() == active_id)
        result.append(pw)
    self._json({'wallets': result})
```

### 3c: Add POST /api/wallets/switch

- [ ] **Step 5: Add to do_POST before the final `else: self.send_error(404)`**

```python
if path == '/api/wallets/switch':
    length = int(self.headers.get('Content-Length', 0))
    try:
        body = json.loads(self.rfile.read(length)) if length else {}
    except Exception:
        body = {}
    wallet_id = body.get('id')
    if not wallet_id:
        self._json({'error': 'id required'}, 400)
        return
    ok, err = _wallet_mgr.switch_context(wallet_id)
    if not ok:
        self._json({'error': err}, 400)
        return
    _clear_all_caches()
    state.init_db()   # ensure new state DB is initialised
    self._json({'ok': True, 'wallet_id': wallet_id,
                'address': os.environ.get('WALLET_ADDRESS', '')})
    return
```

- [ ] **Step 6: Smoke test API**

Start serve_dashboard.py and in another terminal:
```powershell
python serve_dashboard.py
# In another PowerShell window:
Invoke-WebRequest -Uri http://localhost:8766/api/wallets -UseBasicParsing | Select-Object -ExpandProperty Content
```
Expected: JSON with two wallet objects, no `private_key` fields, `is_active: true` on test wallet.

---

## Task 4: Add wallet tabs to dashboard.html

**Files:**
- Modify: `C:\Users\Admin\base-agent\dashboard.html`

### 4a: CSS for wallet tabs

- [ ] **Step 1: Add CSS after the `.btn-refresh` block (around line 64)**

```css
/* ── Wallet tabs ── */
.wallet-tabs {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
}
.w-tab {
  font-family: var(--mono); font-size: 11px;
  background: transparent; border: 1px solid var(--border);
  border-radius: 4px; padding: 4px 12px; cursor: pointer;
  color: var(--muted); transition: all .15s;
}
.w-tab:hover { border-color: var(--cyan); color: var(--text); }
.w-tab.active {
  border-color: var(--cyan); color: var(--cyan);
  background: rgba(0,212,255,.08);
  box-shadow: 0 0 6px rgba(0,212,255,.25);
}
.w-tab-add {
  color: var(--green); border-color: var(--green);
  padding: 4px 8px;
}
.w-tab-add:hover { background: rgba(0,255,136,.08); }
```

### 4b: HTML — add wallet tabs inside the header

- [ ] **Step 2: Find the `.hdr` div in dashboard.html (around line 605)**

The current header looks like:
```html
<div class="hdr">
  <div class="hdr-left"> ... </div>
  <div class="hdr-right"> ... </div>
</div>
```

Add wallet tabs div BETWEEN `hdr-left` and `hdr-right`:
```html
  <div class="wallet-tabs" id="wallet-tabs">
    <!-- populated by JS -->
  </div>
```

So the full hdr becomes:
```html
<div class="hdr">
  <div class="hdr-left">...</div>
  <div class="wallet-tabs" id="wallet-tabs"></div>
  <div class="hdr-right">...</div>
</div>
```

### 4c: JavaScript — fetchWallets + switchWallet

- [ ] **Step 3: Add JS functions before the `loadAll()` call at bottom of script**

Add these functions:

```javascript
// ── Wallet tabs ───────────────────────────────────────────────────────
let _walletSwitching = false;

async function fetchWallets() {
  try {
    const r = await fetch('/api/wallets');
    const data = await r.json();
    renderWalletTabs(data.wallets || []);
  } catch (e) {
    console.warn('fetchWallets error:', e);
  }
}

function renderWalletTabs(wallets) {
  const container = document.getElementById('wallet-tabs');
  if (!container) return;
  container.innerHTML = '';
  wallets.forEach(w => {
    const btn = document.createElement('button');
    btn.className = 'w-tab' + (w.is_active ? ' active' : '');
    btn.textContent = w.name;
    btn.dataset.id = w.id;
    btn.addEventListener('click', () => switchWallet(w.id));
    container.appendChild(btn);
  });
}

async function switchWallet(id) {
  if (_walletSwitching) return;
  _walletSwitching = true;
  // Optimistically update tab style
  document.querySelectorAll('.w-tab').forEach(b => {
    b.classList.toggle('active', b.dataset.id === id);
  });
  try {
    const r = await fetch('/api/wallets/switch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id}),
    });
    const data = await r.json();
    if (data.error) {
      console.error('Switch wallet error:', data.error);
      // Revert tabs on error
      await fetchWallets();
      return;
    }
    // Reload all panels with new wallet data
    await loadAll();
  } catch (e) {
    console.error('switchWallet fetch error:', e);
    await fetchWallets();
  } finally {
    _walletSwitching = false;
  }
}
```

- [ ] **Step 4: Call fetchWallets() inside loadAll()**

Find `async function loadAll()` and add `fetchWallets()` call at the top of it:

```javascript
async function loadAll() {
  fetchWallets();   // add this line
  // ... existing code ...
}
```

- [ ] **Step 5: Verify in browser**

Start serve_dashboard.py. Open `http://localhost:8766/dashboard.html`.

Verify:
1. Two wallet tabs appear in header: **Test Wallet** (active, cyan glow) and **Ifond**
2. Clicking "Ifond" → tab highlights, panels reload with 0 positions (empty state DB) and ifond address
3. Clicking "Test Wallet" → switches back, positions reload

---

## Task 5: Ensure state_test.db exists (rename existing state.db)

Current `state.db` contains the test wallet's positions. After the switch to wallets.json, the test wallet's state_db field points to `state_test.db`.

**Files:**
- No new files — just a copy/rename step

- [ ] **Step 1: Copy state.db → state_test.db**

```powershell
Copy-Item "C:\Users\Admin\base-agent\state.db" "C:\Users\Admin\base-agent\state_test.db"
```

- [ ] **Step 2: Verify by switching to test wallet in dashboard**

After restart, test wallet tab should show the same positions as before.

- [ ] **Step 3: Update .gitignore**

Check `C:\Users\Admin\base-agent\.gitignore`. Ensure it contains:
```
state*.db
wallets.json
```

If not, add both lines.

---

## Task 6: Commit

- [ ] **Step 1: Stage new/modified files (exclude wallets.json — it has private keys)**

```powershell
git -C "C:\Users\Admin\base-agent" add wallet_manager.py serve_dashboard.py dashboard.html .gitignore docs/
git -C "C:\Users\Admin\base-agent" status
```

Verify `wallets.json` and `state_test.db` do NOT appear in staged files.

- [ ] **Step 2: Commit**

```powershell
git -C "C:\Users\Admin\base-agent" commit -m "feat: Phase MW-1 multi-wallet tab UI

- wallet_manager.py: load/save/switch_context (hot-swap executor + state)
- wallets.json: test + ifond wallet registry (gitignored, private keys)
- serve_dashboard.py: /api/wallets GET + /api/wallets/switch POST + cache clear
- dashboard.html: wallet tab bar in header, switchWallet() + fetchWallets() JS"
```

---

## Self-Review

**Spec coverage:**
- ✅ `wallets.json` registry with test + ifond — Task 1
- ✅ Tab bar [Test] [Ifond] — Task 4
- ✅ Click tab → switch context → dashboard reloads with new wallet — Tasks 3+4
- ✅ `active` field on wallet (used for `is_active` in API response) — Task 2
- ✅ Persist last_active across restarts — Task 2 (`save_wallets` + `get_last_active`) + Task 3 (startup init)
- ✅ Private key hidden from browser API — `public_wallet()` in wallet_manager.py
- ✅ state_test.db has existing positions — Task 5

**Not yet done (later phases):**
- `+` add wallet button (modal form) — punted to post-MW-1
- Stop/Play toggle — MW-2
- agent.py loop — MW-2

**Placeholder scan:** No TBDs or unimplemented stubs found.

**Type consistency:** `wallet_manager.switch_context(wallet_id)` returns `(bool, str|None)` — consistent across Task 2, 3a, 3c.
