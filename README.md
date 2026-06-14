# Base Airdrop Agent

Automated DeFi farming agent for Base chain. Supplies, borrows, and manages positions across 88+ platforms to maximize on-chain activity for potential airdrops.

## Supported Platforms

**Lending/Supply:** Compound v3, Fluid, Moonwell, Spark, Morpho, Beefy, AAVE v3  
**Borrowing:** Compound v3 Borrow, Fluid T1 Vault, Moonwell Borrow, AAVE v3 Borrow  
**LP:** Aerodrome (9 pools), Uniswap v3 (10 pools), PancakeSwap v3 (13 pools)  
**Other:** Aerodrome veAERO vote, ERC20 deploy, Megapot lottery

## Requirements

- Python 3.10+
- Base mainnet wallet with ETH
- (Optional) Alchemy API key for pool discovery

## Setup

```bash
git clone <repo>
cd base-agent
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env — fill in WALLET_ADDRESS, WALLET_PRIVATE_KEY, etc.

# Configure wallets
cp wallets.json.example wallets.json
# Edit wallets.json — add your wallet(s)
```

## Quick Start (Windows)

### Step 1 — Install dependencies
```bash
pip install -r requirements.txt
```

### Step 2 — Get an Alchemy API key (free)

1. Go to [https://www.alchemy.com](https://www.alchemy.com) → **Sign up** (free account)
2. Click **Create new app** → choose **Base** as the network
3. Copy the **HTTPS** endpoint URL (looks like `https://base-mainnet.g.alchemy.com/v2/YOUR_KEY`)
4. Paste it into `.env` as `DISCOVERY_RPC_URL=<your URL>`

> Without this, LP pool discovery is skipped — agent still works but may miss some Uniswap/PancakeSwap pools.

### Step 3 — Configure environment
```bash
cp .env.example .env
# Edit .env — fill in WALLET_ADDRESS, WALLET_PRIVATE_KEY, and DISCOVERY_RPC_URL
```

### Step 4 — Configure wallets
```bash
cp wallets.json.example wallets.json
# Edit wallets.json — add your wallet (id, address, private_key)
```

### Step 5 — Launch
Double-click **`start.bat`** — opens Agent CMD + Dashboard CMD + browser automatically.

```
start.bat
```

> `.env` and `wallets.json` are **not included in the repo** (contain private keys). Steps 3–4 are required before first run.

## Running (Manual)

```bash
# Start dashboard (localhost:8766)
python serve_dashboard.py

# Run agent (daily scheduler)
python agent.py

# Manual: supply 1-2 random platforms now
python run_now.py

# Dry run (no real TX)
DRY_RUN=true python run_now.py

# Check active positions
python check_positions.py

# Withdraw all positions → ETH
python withdraw_all.py
```

## Dashboard

Open `http://localhost:8766/dashboard.html` after starting `serve_dashboard.py`.

Features:
- Live position values (USD)
- Today's plan + schedule
- Health monitor (borrow safety)
- Multi-wallet support
- Action log + rule validation log

## Configuration

All config in `.env` (copy from `.env.example`):

| Variable | Description |
|---|---|
| `WALLET_ADDRESS` | Your wallet address |
| `WALLET_PRIVATE_KEY` | Your private key |
| `BASE_RPC_URL` | Base RPC (default: mainnet.base.org) |
| `DISCOVERY_RPC_URL` | Alchemy RPC for pool discovery (optional) |
| `MIN_ETH_BALANCE` | Stop if ETH below this (gas reserve) |
| `DRY_RUN` | `true` = simulate only, no TX |
| `DASHBOARD_PIN` | Dashboard action PIN (default: 0000) |
| `DASHBOARD_ADMIN_PIN` | Dashboard admin PIN (default: 0000) |

## Safety

- Positions are capped at ~$5-15 USD each
- Health monitor closes borrows automatically if health < 1.5x
- `MIN_ETH_BALANCE` reserves ETH for gas at all times
- All TX use 5x gas buffer to prevent drops
- On-chain recovery reconciles DB with chain state on every startup

## Multi-Wallet

Add wallets via the dashboard ⊕ button, or manually edit `wallets.json` (see `wallets.json.example`).

Agent currently executes one wallet at a time. Multi-wallet parallel execution is planned.

## Changelog

### 2026-06-09

#### AI Watcher Phase 2b — Live Auto-Remediation

After Sonnet diagnoses an incident (Phase 2a), Phase 2b can automatically run recovery scripts — no human click needed.

**How it works:**
- `REMEDIATION_MODE=live` (set in `start.bat` for the watcher window) activates live remediation
- After diagnosis: if `category=state_drift` AND `auto_fixable=true` AND `confidence>=medium` → watcher immediately runs a deterministic recovery command
- Recovery commands are a hard whitelist — no second AI call, no creative actions: only `withdraw_all`, `reconcile`, or `sweep_tokens`
- Bounded to `MAX_REMEDIATION_ACTIONS=3` per incident; all actions logged to incident store and shown on dashboard

**Ghost position proactive scan:** watcher now checks every active DB row against on-chain balance on every poll cycle. If on-chain balance is 0 for a position that DB says is active, it creates a `withdraw_failed` incident immediately — no need to wait for the bot to fail at withdrawal. Covered platform types: erc4626, ctoken, beefy_single, beefy_lp, uni_lp, pancake_lp, aero_lp (checks gauge balance), aero_vote (checks `locked()` struct).

**Cross-DB cleanup (watcher):** every poll cycle runs two passes — (1) if a position is closed in any wallet's DB, close the matching row (same platform+entry_date) in all other DBs to prevent ghost-retry loops; (2) delete closed rows with expiry older than `POSITION_CLEANUP_DAYS` (default 7).

**Manual withdraw loop guard:** watcher now checks if a `manual_withdraw_*.json` entry's position is already closed in DB before re-flagging — prevents the same stuck-withdraw from generating repeated incidents after the user manually closed it.

**Sonnet auto-resolve:** if diagnosis category is `external` or `unknown` but root cause text contains self-resolved/already-resolved keywords (e.g. bot successfully repicked), the incident is auto-resolved without user action.

#### AI Watcher Phase 3 — Code-Fix Agent + Dashboard Approve/Reject

When the watcher diagnoses `category=code_bug`, Phase 3 queues a Sonnet-powered code-fix agent to patch the code automatically — with a mandatory human review gate before anything touches the live codebase.

**`code_fix_agent.py` (new file):**
- Creates a git worktree at `cache/fix-{incident_id}/` (isolated copy of the repo — Sonnet cannot touch the live files)
- Runs Sonnet inside the worktree with tools: Read, Grep, Glob, Edit, Write, Bash (restricted to `py_compile` only — no TX scripts, no fund-touching)
- `permission_mode=bypassPermissions` — required because headless Claude Agent SDK has no TTY and the default mode auto-denies Edit/Write
- After edit: runs `py_compile.compile()` to verify syntax
- Computes `git diff` of the worktree vs HEAD, stores it in `incident.proposal.patch_diff`
- Sets incident status to `needs_approval`

**Dashboard Approve/Reject UI:**
- Incident card shows a syntax-highlighted diff `<pre>` block when `status=needs_approval`
- **✅ Approve** button → `POST /api/fix_approve` → `code_fix_agent.apply_fix()` — copies changed files from worktree to live repo (creates `.bak` backup), removes worktree, marks incident resolved
- **✕ Reject** button → `POST /api/fix_reject` → `code_fix_agent.reject_fix()` — removes worktree, marks incident dismissed
- **✓ DISMISS** button — appears on `needs_manual` incidents; marks resolved without running any fix

**Bot short-circuit (deterministic revert detection):**
- Every `_action_log(..., step='fail')` call now passes the full exception string to `_check_deterministic_revert()`
- Extracts 4-byte selector (pattern `0x[0-9a-f]{8}`) from the error; counts per-platform in `cache/revert_counts.json`
- If same selector seen ≥ 2 times on same platform → fires a `deterministic_revert` incident → platform is immediately excluded from candidate selection in both `daily_job` and `_the_rule_repick` until the incident is resolved
- Prevents the bot from repeatedly wasting gas on a platform that will always revert (e.g. ABI changed, reserve frozen, wrong address)

**Other fixes (2026-06-09):**
- `state.py` `close_position()` now cross-syncs to all other `state*.db` files — if you close a position in wallet A's DB, the same platform+entry_date row is also closed in wallet B's DB
- `state.py` `DB_PATH` no longer hardcodes `state.db` — resolves via `wallet_manager.get_last_active()` so the module works correctly when called from scripts that haven't set `STATE_DB_PATH`
- `serve_dashboard.py` `/api/close_position` — new endpoint to manually mark any DB row as closed (useful when AI watcher needs to resolve a ghost without running on-chain TX)
- `serve_dashboard.py` `/api/incident_dismiss` — mark any open incident resolved directly from dashboard
- `serve_dashboard.py` `aero_lp` USD display — reads on-chain gauge `balanceOf` instead of stored `amount_wei` (stored value was ETH budget, not LP token wei — was causing wrong USD display)
- `watcher.py` now calls `load_dotenv()` at startup so `REMEDIATION_MODE` in `.env` is respected when watcher is launched directly (not via `start.bat`)
- `start.bat` sets `REMEDIATION_MODE=live` in the watcher window — Phase 2b+3 active by default on launch

#### To activate Phase 2b+3

No code changes needed. Set in `.env` or `start.bat`:
```
REMEDIATION_MODE=live
```
Default (`diagnose`) keeps Phase 2a (read-only diagnosis). `live` adds auto-remediation + code-fix dispatch.

---

### 2026-06-06
- **start.bat**: launcher script — opens Agent CMD + Dashboard + browser in one click
- **start.bat**: kill only Base Agent/Dashboard windows, not all Python processes
- **aero_vote on-chain guard**: before entering new lock, scan wallet's veAERO NFTs on-chain via `ownerOf` range scan + `balanceOf` check — prevents duplicate positions even if DB is out of sync
- **aero_vote orphan reconcile**: if NFT found on-chain but missing in DB, auto-insert into DB and skip new enter
- **aero_vote price sanity**: reject if AERO price is outside \$0.05–\$50 range (guards against stale oracle returning wrong price)
- **aero_vote USD cap**: hard cap at \$10 USD per lock regardless of config value
- **aero_vote lock formula fix**: corrected epoch rounding — `lock_days=7` now locks exactly 1 epoch (~7 days) instead of 2 epochs (~14 days) due to off-by-one in `+1` formula
- **aero_vote in random candidate pool**: ENTER is randomly selected by the rule engine like any other platform — no forced re-entry after exit; maintenance job handles EXIT only
- **swap STF fix**: `swap_token_to_eth` now reads actual on-chain token balance before swap and caps `amount_in_wei` to it — prevents `SafeTransferFrom` revert when protocol fees cause withdrawn amount to be less than what state.db recorded
- **swap dust skip**: skip swap if DEX quote output < 0.0001 ETH (~$0.35) to avoid wasting gas on dust amounts
- **429 RPC fix**: agent now uses Alchemy (`DISCOVERY_RPC_URL`) for all TX calls when configured, eliminating `Too Many Requests` errors on public mainnet.base.org — **requires agent restart to take effect**
- **daily_job withdraw crash fix**: moved `int(float(amount_wei))` parse inside the try/except in `daily_job` (run_now.py path) — a non-numeric `amount_wei` (aero_vote `tokenId|wei`, psm_hold `psm_hold_<bal>`) no longer raises an uncaught `ValueError` that aborted the entire expired-withdraw loop
- **expiry override fix**: lend/aero_lp/beefy supply types now honor the plan's custom expiry instead of recomputing the default — the loop only recomputes expiry on a repick (when the override no longer applies)
- **aero_vote address fix**: corrected `aero_vote.address` in contracts.json to the canonical VotingEscrow `0xeBf418Fe2512e7E6...` (was a wrong address only referenced by debug scripts; execution path already used the correct one)
- **erc4626 dust fix**: `erc4626_withdraw_all` now uses `redeem(shares)` instead of `withdraw(convertToAssets(shares))` — burns the exact share balance, returns all underlying, leaves zero dust shares, and avoids the rounding edge that can revert "withdraw more than max"
- **compound dust fix**: `compound_withdraw` now reads the current Comet `balanceOf` (principal + accrued interest) and withdraws that, instead of the originally-deposited amount — leaves zero dust in the protocol
- **RPC fallback rotation**: on a transient error (429 / timeout / connection / 5xx) the agent rotates the active provider to the next endpoint instead of just sleeping on the saturated one. Priority: `DISCOVERY_RPC_URL` (Alchemy) → `BASE_RPC_URL` → `BASE_RPC_FALLBACKS` (new, comma-separated). Rotation is sticky and global — once a healthy endpoint is found, all subsequent calls use it. Wired into `_rpc_call`, `_send` (TX submit), and `_gas_limit`
- **read-after-write sync fix**: replaced blind `time.sleep(4)` after TXs with `wait_for_sync()` — captures the confirmed TX's block number and polls until the read node reports a height ≥ that block, fixing stale `balanceOf` reads caused by load-balanced RPC replica lag (a read can hit a node 1-2 blocks behind the mined TX). Returns early when synced, waits longer when the node lags. Applied across deposit (LP/erc4626), swap unwrap, and the full withdraw path
- **AI Watcher (Phase 1 — detection + dashboard)**: new `watcher.py` process polls the signal files the bot already writes (`maintenance_done_*`, `manual_withdraw_*`, `action_log_*`) and records incidents into `cache/incidents.json` via `incident_store.py`. The dashboard shows a new **🛠 AI WATCHER** panel with a live agent-state chip (idle/watching/working/waiting/offline) and a deduplicated incident list with recurrence tracking (escalates to critical after 3 distinct days). Read-only, zero changes to the bot core. `start.bat` now launches a 3rd window for the watcher.
- **AI Watcher (Phase 2a — Sonnet diagnosis, read-only)**: `remediation_agent.py` runs Sonnet (via the Claude Agent SDK, subscription auth — no API key) on each new incident to produce a structured diagnosis: root cause, category (state_drift / external / code_bug / unknown), proposed recovery action, and auto-fixable flag. **Diagnose-only — read-only tools (Read/Grep/Glob), no Bash/Edit/Write, cannot touch funds or code.** Extended thinking disabled + trimmed prompt for ~60s bounded runs. The watcher triggers it (bounded by `DIAGNOSE_PER_SCAN`), and the dashboard card shows the diagnosis with a category badge. Verified live: correctly diagnosed the stuck `aave_cbbtc#30` as an **external** AAVE-reserve condition (not a code bug / not state drift), `auto_fixable=false` → "wait, don't force-close". Live remediation (actually running recovery scripts) is Phase 2b behind `REMEDIATION_MODE=live`; code-fix proposals + dashboard approve are Phase 3.

## License

MIT
