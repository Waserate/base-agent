# Base Agent — Project Context
_Read this first in every session. Updated: 2026-06-09_

---

## Architecture in One Paragraph

Multi-wallet Base-chain DeFi airdrop bot. `agent.py` executes daily actions (lend, LP, vote, etc.) per wallet using `executor.py` (Web3 calls) and records positions in per-wallet SQLite DBs. `watcher.py` polls every 300s for failures, creates incidents in `cache/incidents.json`, calls `remediation_agent.py` (Sonnet) to diagnose, then auto-fixes state_drift or flags for human. `serve_dashboard.py` serves a browser dashboard at port 8888 showing positions, incidents, briefing, and stats.

---

## Files

| File | Role |
|------|------|
| `agent.py` | Main bot loop — per-wallet daily action executor |
| `executor.py` | All Web3 calls (supply/withdraw/LP/vote), price feeds, swap |
| `state.py` | SQLite CRUD for positions + daily stats |
| `wallet_manager.py` | Wallet routing — `switch_context(wid)` sets env + reloads modules |
| `watcher.py` | Incident detector + diagnosis/remediation orchestrator |
| `incident_store.py` | `cache/incidents.json` CRUD — dedup, status FSM |
| `remediation_agent.py` | Sonnet diagnose + deterministic remediation whitelist |
| `serve_dashboard.py` | HTTP server (port 8888) + all `/api/*` endpoints |
| `dashboard.html` | Single-file browser UI |
| `dispatcher.py` | Per-platform action dispatcher (called by agent) |
| `rule_engine.py` | Platform eligibility rules (cooldown, slots, cap) |
| `onchain_recovery.py` | Scan on-chain for positions not in DB |
| `withdraw_all.py` | Withdraw expired/stuck positions |
| `config/contracts.json` | All platform configs, token addresses, ABIs |
| `wallets.json` | Wallet registry (id, address, state_db, private_key) |

---

## Wallets & DBs

| Wallet ID | Address | DB file | Notes |
|-----------|---------|---------|-------|
| `test` | `0xfEA3BEfE...` | `state_test.db` | Primary / last_active |
| `ifond` | `0xA391a90c...` | `state_ifond.db` | Secondary |

`state.db` — **deleted**. Code no longer references it.

`DB_PATH` resolution order in `state.py`:
1. `STATE_DB_PATH` env var (set by `wallet_manager.switch_context()`)
2. `wallet_manager.get_last_active()` → last_active wallet's DB
3. `RuntimeError` if no wallet in wallets.json

---

## DB Schema — `positions` table

```sql
id          INTEGER PRIMARY KEY
platform    TEXT          -- e.g. 'morpho_eth', 'aero_vote', 'mw_aero_usdc'
token       TEXT          -- e.g. 'WETH', 'AERO', 'cbBTC'
amount_wei  TEXT          -- raw value; format varies by platform type (see below)
entry_date  TEXT          -- ISO date YYYY-MM-DD
expiry_date TEXT          -- ISO date YYYY-MM-DD
tx_hash     TEXT
status      TEXT          -- 'active' | 'closed'
opened_usd  REAL
closed_usd  REAL
gas_cost_wei INTEGER
```

---

## amount_wei Formats by Platform Type

| Type | Format | Example |
|------|--------|---------|
| `erc4626` | vault shares (raw int) | `30174821186134` |
| `ctoken` | cToken shares (raw int) | `17860830` |
| `aero_vote` | `tokenId\|aeroWei` | `120933\|87351763875861779648` |
| `aero_lp` / `uni_lp` / `pancake_lp` | LP token wei | `10000000000000000` |
| `beefy_lp` | mooToken shares | `4920000000000000000` |
| `beefy_single` | mooToken shares | raw int |
| `comet` | underlying wei | raw int |
| `psm_hold` | underlying wei | raw int |
| `mw_borrow` / `aave_borrow` | `COL_SYM:wei\|COL_SYM:wei\|\|BOR_SYM:wei[:mtoken:addr]` | |
| `fluid_borrow` | `nftId:N\|\|COL:sym:wei\|\|BOR:sym:wei` | |
| `cb_aero_weth` | `TOKEN:wei\|\|TOKEN:wei` | `WETH:3352789649561008\|\|AERO:3206...` |
| `mw_aero_usdc` | `TOKEN:wei\|\|TOKEN:wei\|\|BOR_SYM:wei:mtoken:addr` | |
| `deploy_contract` | deployed contract address | `0xA9b2...` |

---

## Platform Type → USD Calculation (serve_dashboard.py `_live_usd_est`)

| ptype | How USD is calculated |
|-------|----------------------|
| `erc4626` | `previewRedeem(shares)` → underlying → `× price` |
| `ctoken` | `balanceOf(wallet) × exchangeRateStored / 1e18 × price` |
| `aero_vote` | `aeroWei / 1e18 × AERO_price` |
| `beefy_single` | `balanceOf × getPricePerFullShare / 1e18 × price` |
| `comet` | `amount_wei / 10^dec × price` |
| `aero_lp` / `uni_lp` / `pancake_lp` / `beefy_lp` | on-chain reserves × share ratio → token amounts → USD |
| `mw_borrow` / `aave_borrow` | col_usd − debt_usd |

---

## Incident FSM

```
detected → diagnosed → remediated → resolved
                    ↘ needs_manual (external/unknown, human required)
                    ↘ resolved (external but self-resolved, auto-close)
```

Key fields: `status`, `category`, `auto_fixable`, `confidence`, `proposal.proposed_action`

Categories: `state_drift` | `code_bug` | `external` | `unknown`

Dedup key: `signal:wallet:platform:pos_id` — non-resolved incidents always update in-place (no duplicate creation regardless of cooldown).

---

## Watcher Pipeline (every POLL_S=300s)

```
scan_once()
  _scan_maintenance()       — maintenance_done_<date>.json partial failures
  _scan_manual_withdraw()   — manual_withdraw_<wid>.json stuck positions
  _scan_action_log()        — action_log_<wid>.json fail/repick/recovery steps
  _scan_positions_onchain() — proactive ghost detection (all active DB rows)
  _run_diagnoses()          — diagnose 'detected' incidents via Sonnet
  _run_remediations()       — sweep 'diagnosed' auto_fixable (after restart)
  _cleanup_closed_positions() — Pass1: cross-DB sync | Pass2: age purge
```

---

## Remediation Flow (remediation_agent.py)

```
diagnose(incident_id)
  1. _ghost_check(inc)      — on-chain balance=0 → state_drift, skip Sonnet
  2. _run(inc)              — Sonnet diagnosis (read-only tools only)
  3. store.update()         — writes category, auto_fixable, confidence top-level

remediate(incident_id)
  MODE=live + state_drift + auto_fixable + confidence∈{medium,high}:
    _map_to_commands() → whitelist:
      - withdraw_all.py --id N
      - python -c 'import state; state.close_position(N)'
      - onchain_recovery.reconcile()
    _run_cmd() → subprocess with correct wallet env
    _verify_remediation() → re-check DB status
  MODE=live + code_bug:
    code_fix_agent.run_fix() → git worktree + diff → needs_approval
```

---

## _onchain_balance(platform, wallet) — Supported Types

| Prefix | Method |
|--------|--------|
| `aave_supply` | aToken.balanceOf |
| `ctoken` / `moonwell_*` / `mw_*` | cToken.balanceOf |
| `erc4626` / `morpho_*` / `fluid_*` / `spark_*` | ERC4626.balanceOf |
| `aero_lp` / `uni_lp` / `pancake_lp` | gauge.balanceOf → pool.balanceOf fallback |
| `beefy_lp` | gauge.balanceOf → vault.balanceOf fallback |
| `aero_vote` | VotingEscrow.locked(tokenId) → amount=0 or end<now = ghost |
| `aave_borrow` / `mw_borrow` | debtToken.balanceOf |

---

## Key Env Vars (.env)

```
WALLET_ID            active wallet id
WALLET_ADDRESS       wallet address
WALLET_PRIVATE_KEY   wallet private key
STATE_DB_PATH        absolute path to active wallet's state DB
REMEDIATION_MODE     diagnose (read-only) | live (execute fixes)
REMEDIATION_MODEL    claude-sonnet-4-6 (default)
WATCHER_POLL_S       300 (default)
DIAGNOSE_PER_SCAN    1 (default) — max Sonnet calls per poll cycle
POSITION_CLEANUP_DAYS 7 (default) — delete closed rows older than N days
REMEDIATION_ENABLED  1 (default) — set 0 to disable Phase 2
```

---

## API Endpoints (GET)

| Endpoint | Returns |
|----------|---------|
| `/api/state` | active positions, protocol_summary, type_counts |
| `/api/state/all` | aggregated across all wallets |
| `/api/stats` | daily_stats 30d, type_counts, positions_gas/vol |
| `/api/health` | on-chain balance checks for active positions |
| `/api/health/all` | health across all wallets |
| `/api/incidents` | all incidents + agent_state + remediation_mode |
| `/api/briefing` | today's action plan + schedule |
| `/api/plan` / `/api/plan/all` | planned actions |
| `/api/balance` | ETH + token balances |
| `/api/history` | closed positions last 30 rows |
| `/api/action_log` | recent bot actions |
| `/api/rule_log` | rule engine decisions |
| `/api/wallets` | wallet list (no private keys) |

## API Endpoints (POST)

| Endpoint | Action |
|----------|--------|
| `/api/close_position` | Mark position closed (searches all wallet DBs) |
| `/api/incident_dismiss` | Resolve incident (needs_manual → resolved) |
| `/api/wallets/switch` | Switch active wallet context |
| `/api/emergency_close` | Close all active positions |
| `/api/reconcile` | Run onchain_recovery.reconcile() |
| `/api/reroll_all` | Reroll all planned actions |
| `/api/fix_approve` / `/api/fix_reject` | Accept/reject code_fix_agent diff |

---

## cross-DB Sync (state.py close_position)

`close_position(pos_id)` immediately syncs to all other `state*.db` files:
1. Closes row in current DB
2. `glob state*.db` → UPDATE same platform+entry_date in every other DB
3. watcher `_cleanup_closed_positions()` runs as safety net every 300s

---

## Known Quirks

- **VotingEscrow tokenOfOwnerByIndex**: Aerodrome VotingEscrow does NOT implement ERC721Enumerable. Pass tokenId directly (stored in amount_wei as `tokenId|aeroWei`).
- **veAERO lock #120933**: test wallet, expires 2026-06-19. Lock #120320: test wallet, expired ~2026-06-11.
- **morpho_cbbtc ifond**: pos#16 in state_ifond.db, amount_wei=vault shares (30174821186134), status=active.
- **WETH Balanced Morpho (ifond)**: $9.74 WETH in "Gauntlet WETH Balanced" vault — NOT tracked in state_ifond.db (manually deposited before agent). Different vault from `morpho_eth` (Moonwell Flagship ETH).
- **ERC4626 amount_wei = vault shares** (not underlying). previewRedeem(shares) = underlying.
- **Borrow dedup**: Moonwell+AAVE share-pool — mToken/aToken balance can appear in both; deduplicate by borrow_token.
- **Incident dedup key** includes pos_id — same platform different pos_id = separate incident.
- **manual_withdraw_<wid>.json has no cursor/TTL** — entries persist forever. If a position was already closed but still listed in JSON, old watcher created a new incident every 300s (closed incident → new incident, infinite loop). Fixed: `_pos_still_active()` in `_scan_manual_withdraw()` skips entries where DB row is already closed. When debugging rediagnose loop: check JSON for stale closed pos_ids.

---

## Running Processes (normal operation)

```
python agent.py           # main bot (background)
python serve_dashboard.py # dashboard HTTP server port 8888
python watcher.py         # incident detector (background)
```

All 3 read `.env` via `load_dotenv()` at startup.
