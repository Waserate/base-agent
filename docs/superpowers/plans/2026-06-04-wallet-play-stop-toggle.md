# Wallet Play/Stop Toggle (Phase MW-2a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Play/Stop toggle button beside each wallet tab so the user can mark a wallet as active (running) or paused — persisted in `wallets.json` for when the multi-wallet agent loop is ready.

**Architecture:** Each wallet tab gets a sibling toggle button. Clicking the toggle POSTs to `/api/wallets/toggle`, which flips `active` in `wallets.json` and returns the new state. Dashboard re-renders the tabs. The wallet tab button (name) still switches the dashboard context; only the toggle button changes the `active` flag. `active: false` dims the tab visually.

**Tech Stack:** Python stdlib (json, os), vanilla JS in dashboard.html, existing `wallet_manager.py` + `serve_dashboard.py`.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `wallet_manager.py` | Modify | Add `toggle_active(wallet_id)` |
| `serve_dashboard.py` | Modify | Add `POST /api/wallets/toggle` endpoint |
| `dashboard.html` | Modify | CSS for `.w-toggle`, `.w-tab-group`, `.w-tab.paused`; update `renderWalletTabs()`; add `toggleWallet()` JS |

---

## Task 1: Add toggle_active() to wallet_manager.py

**Files:**
- Modify: `C:\Users\Admin\base-agent\wallet_manager.py`

- [ ] **Step 1: Add toggle_active() at the end of wallet_manager.py**

Append this function after the existing `switch_context()` function:

```python
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
```

- [ ] **Step 2: Smoke test**

Run in PowerShell from `C:\Users\Admin\base-agent\`:
```powershell
python -c "
import wallet_manager
ok, new_val, err = wallet_manager.toggle_active('test')
print(ok, new_val, err)
# Toggle back
ok2, new_val2, err2 = wallet_manager.toggle_active('test')
print(ok2, new_val2, err2)
"
```
Expected output:
```
True False None
True True None
```

---

## Task 2: Add POST /api/wallets/toggle to serve_dashboard.py

**Files:**
- Modify: `C:\Users\Admin\base-agent\serve_dashboard.py`

- [ ] **Step 1: Locate the POST /api/wallets/switch block**

Find the `if path == '/api/wallets/switch':` block in `do_POST()`. The new toggle endpoint goes AFTER it (before the final `else: self.send_error(404)`).

- [ ] **Step 2: Add the toggle endpoint**

Insert after the `if path == '/api/wallets/switch':` block's closing `return`:

```python
        if path == '/api/wallets/toggle':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length)) if length else {}
            except Exception:
                body = {}
            wallet_id = body.get('id')
            if not wallet_id:
                self._json({'error': 'id required'}, 400)
                return
            ok, new_active, err = _wallet_mgr.toggle_active(wallet_id)
            if not ok:
                self._json({'error': err}, 400)
                return
            self._json({'ok': True, 'id': wallet_id, 'active': new_active})
            return
```

- [ ] **Step 3: Verify syntax**

```powershell
python -c "import ast; ast.parse(open('serve_dashboard.py', encoding='utf-8').read()); print('OK')"
```
Expected: `OK`

---

## Task 3: Update dashboard.html — CSS + HTML + JS

**Files:**
- Modify: `C:\Users\Admin\base-agent\dashboard.html`

### 3a: CSS

- [ ] **Step 1: Add CSS for play/stop toggle button and paused tab**

Find the existing `.w-tab.active { ... }` block (around line 77-81) and add these rules AFTER it:

```css
    .w-tab.paused { opacity: 0.45; }
    .w-tab-group { display: flex; align-items: center; gap: 2px; }
    .w-toggle {
      font-size: 9px; line-height: 1;
      background: transparent; border: 1px solid var(--border);
      border-radius: 3px; padding: 3px 6px; cursor: pointer;
      color: var(--muted); transition: all .15s;
    }
    .w-toggle.running { color: var(--green); border-color: var(--green); }
    .w-toggle.running:hover { background: rgba(0,255,136,.1); }
    .w-toggle.stopped { color: var(--muted); border-color: var(--border); }
    .w-toggle.stopped:hover { color: var(--cyan); border-color: var(--cyan); }
```

### 3b: JS — renderWalletTabs update

- [ ] **Step 2: Replace the existing renderWalletTabs() function**

Find the existing `function renderWalletTabs(wallets)` function (around line 1635) and replace it entirely with:

```javascript
function renderWalletTabs(wallets) {
  const container = document.getElementById('wallet-tabs');
  if (!container) return;
  container.innerHTML = '';
  wallets.forEach(w => {
    const group = document.createElement('div');
    group.className = 'w-tab-group';

    // Name tab (switches dashboard context)
    const tab = document.createElement('button');
    tab.className = 'w-tab' + (w.is_active ? ' active' : '') + (w.active === false ? ' paused' : '');
    tab.textContent = w.name;
    tab.dataset.id = w.id;
    tab.addEventListener('click', () => switchWallet(w.id));

    // Play/Stop toggle
    const tog = document.createElement('button');
    const isRunning = w.active !== false;
    tog.className = 'w-toggle ' + (isRunning ? 'running' : 'stopped');
    tog.textContent = isRunning ? '■' : '▶';
    tog.title = isRunning ? 'Stop wallet' : 'Start wallet';
    tog.dataset.id = w.id;
    tog.addEventListener('click', (e) => { e.stopPropagation(); toggleWallet(w.id); });

    group.appendChild(tab);
    group.appendChild(tog);
    container.appendChild(group);
  });
}
```

### 3c: JS — toggleWallet function

- [ ] **Step 3: Add toggleWallet() function**

Add this function right AFTER the `switchWallet()` function (which ends around line 1675):

```javascript
async function toggleWallet(id) {
  try {
    const r = await fetch('/api/wallets/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id}),
    });
    const data = await r.json();
    if (data.error) { console.error('toggleWallet error:', data.error); return; }
    await fetchWallets();
  } catch (e) {
    console.error('toggleWallet fetch error:', e);
  }
}
```

---

## Task 4: Commit

- [ ] **Step 1: Stage files**

```powershell
git -C "C:\Users\Admin\base-agent" add wallet_manager.py serve_dashboard.py dashboard.html
git -C "C:\Users\Admin\base-agent" status --short
```

Expected: `M wallet_manager.py`, `M serve_dashboard.py`, `M dashboard.html`

- [ ] **Step 2: Commit**

```powershell
git -C "C:\Users\Admin\base-agent" commit -m @'
feat: MW-2a wallet play/stop toggle per tab

- wallet_manager.toggle_active(): flip active flag in wallets.json
- serve_dashboard: POST /api/wallets/toggle endpoint
- dashboard: Play/Stop button per wallet tab, dimmed paused state
'@
```

---

## Self-Review

**Spec coverage:**
- ✅ Play/Stop button per wallet tab — Task 3
- ✅ `active` field toggled in wallets.json — Task 1
- ✅ API endpoint `/api/wallets/toggle` — Task 2
- ✅ Visual: paused tab dimmed, running tab shows ■ green, stopped shows ▶ muted — Task 3 CSS
- ✅ Clicking toggle doesn't accidentally switch wallet (stopPropagation) — Task 3b

**Not in scope:**
- agent.py reading `active` flag per wallet loop (MW-2 full)
- Per-wallet plan/log separation (MW-2 full)

**Placeholder scan:** No TBDs found.

**Type consistency:** `toggle_active()` returns `(bool, bool|None, str|None)` — used consistently in Task 1 and Task 2.
