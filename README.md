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

### Step 2 — Configure environment
```bash
cp .env.example .env
# Edit .env — fill in WALLET_ADDRESS and WALLET_PRIVATE_KEY at minimum
```

### Step 3 — Configure wallets
```bash
cp wallets.json.example wallets.json
# Edit wallets.json — add your wallet (id, address, private_key)
```

### Step 4 — Launch
Double-click **`start.bat`** — opens Agent CMD + Dashboard CMD + browser automatically.

```
start.bat
```

> `.env` and `wallets.json` are **not included in the repo** (contain private keys). Steps 2–3 are required before first run.

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

### 2026-06-06
- **start.bat**: launcher script — opens Agent CMD + Dashboard + browser in one click
- **start.bat**: kill only Base Agent/Dashboard windows, not all Python processes
- **aero_vote on-chain guard**: before entering new lock, scan wallet's veAERO NFTs on-chain via `ownerOf` range scan + `balanceOf` check — prevents duplicate positions even if DB is out of sync
- **aero_vote orphan reconcile**: if NFT found on-chain but missing in DB, auto-insert into DB and skip new enter
- **aero_vote price sanity**: reject if AERO price is outside \$0.05–\$50 range (guards against stale oracle returning wrong price)
- **aero_vote USD cap**: hard cap at \$10 USD per lock regardless of config value
- **aero_vote lock formula fix**: corrected epoch rounding — `lock_days=7` now locks exactly 1 epoch (~7 days) instead of 2 epochs (~14 days) due to off-by-one in `+1` formula
- **aero_vote in random candidate pool**: ENTER is randomly selected by the rule engine like any other platform — no forced re-entry after exit; maintenance job handles EXIT only

## License

MIT
