"""
backfill_stats.py — Rebuild daily_stats gas + volume from on-chain TX receipts.

Run once after a DB wipe or when stats are empty:
  python backfill_stats.py

For each position with a tx_hash:
  - Fetches receipt via Alchemy (w3_read)
  - Calculates gas_cost_wei = gasUsed * effectiveGasPrice
  - Updates positions.gas_cost_wei in state.db
  - Inserts/updates daily_stats.gas_usd and volume_usd per entry_date

Volume: sum of opened_usd per date (fallback $5 when NULL).
Gas USD: gas_cost_wei / 1e18 * current ETH price (approximation for all dates).
"""
import sys, time, logging
from datetime import date

logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv()

import state, executor

state.init_db()

# ── ETH price (single fetch, used as approximation for all dates) ──────────
try:
    eth_price = executor.get_eth_usd_price()
    log.info(f'ETH price: ${eth_price:.0f}')
except Exception as e:
    eth_price = 2000.0
    log.warning(f'ETH price fetch failed ({e}) — using ${eth_price:.0f}')

# ── Fetch all positions ────────────────────────────────────────────────────
all_pos = state.all_positions()
log.info(f'Total positions in DB: {len(all_pos)}')

# Group: {entry_date: {gas_cost_wei_sum, volume_usd_sum}}
from collections import defaultdict
daily = defaultdict(lambda: {'gas_wei': 0, 'vol_usd': 0.0, 'updated': 0, 'skipped': 0})

with_txhash  = [p for p in all_pos if len(p) > 6 and p[6]]
no_txhash    = [p for p in all_pos if not (len(p) > 6 and p[6])]

log.info(f'  With tx_hash : {len(with_txhash)}')
log.info(f'  No  tx_hash  : {len(no_txhash)}  (recovery positions — gas unknown)')
log.info('')

# ── Process positions WITH tx_hash ─────────────────────────────────────────
w3_read = executor.w3_read

for i, pos in enumerate(with_txhash, 1):
    pos_id     = pos[0]
    platform   = pos[1]
    entry_date = pos[4]
    tx_hash    = pos[6]
    opened_usd = pos[8] if len(pos) > 8 else None

    try:
        receipt = w3_read.eth.get_transaction_receipt(tx_hash)
        gas_used   = receipt['gasUsed']
        gas_price  = receipt.get('effectiveGasPrice') or receipt.get('gasPrice', 0)
        gas_wei    = gas_used * gas_price

        # Update position row
        state.update_position_gas(pos_id, gas_wei)

        gas_eth = gas_wei / 1e18
        gas_usd = gas_eth * eth_price
        vol_usd = float(opened_usd) if opened_usd is not None else 5.0

        daily[entry_date]['gas_wei'] += gas_wei
        daily[entry_date]['vol_usd'] += vol_usd
        daily[entry_date]['updated'] += 1

        log.info(f'[{i:3d}/{len(with_txhash)}] {platform:<30} gas={gas_eth:.5f} ETH (${gas_usd:.4f})  vol=${vol_usd:.2f}')
        time.sleep(0.3)

    except Exception as e:
        log.warning(f'[{i:3d}/{len(with_txhash)}] SKIP {platform} tx={tx_hash[:10]}... error={e}')
        daily[entry_date]['skipped'] += 1
        time.sleep(0.3)

# ── Process positions WITHOUT tx_hash (recovery) ───────────────────────────
# Count volume only (no gas data available)
for pos in no_txhash:
    entry_date = pos[4]
    opened_usd = pos[8] if len(pos) > 8 else None
    vol_usd    = float(opened_usd) if opened_usd is not None else 5.0
    daily[entry_date]['vol_usd'] += vol_usd

# ── Write daily_stats ──────────────────────────────────────────────────────
log.info('')
log.info('Writing daily_stats...')
for d, vals in sorted(daily.items()):
    gas_usd = (vals['gas_wei'] / 1e18) * eth_price
    vol_usd = vals['vol_usd']
    state.update_daily_gas_vol(d, gas_usd, vol_usd)
    log.info(f'  {d}  gas=${gas_usd:.4f}  vol=${vol_usd:.2f}  ({vals["updated"]} tx, {vals["skipped"]} skipped)')

log.info('')
log.info('Done. Restart serve_dashboard.py to refresh dashboard.')
