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

## Running

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

## License

MIT
