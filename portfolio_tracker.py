"""
portfolio_tracker.py — Daily USD portfolio snapshot.

Approximation:
  wallet_usd   = ETH balance * ETH/USD + USDC balance
  position_usd = active position count * $5.0 (each position is ~$5 by design)
  total_usd    = wallet_usd + position_usd

Writes result to daily_stats via state.update_daily_portfolio().
Returns dict or None on RPC error.
"""

import logging
import os
import json

from dotenv import load_dotenv
load_dotenv()

import executor
import state

log = logging.getLogger(__name__)

with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as _f:
    _CFG = json.load(_f)

USDC_ADDR = _CFG['tokens']['USDC']['address']
POSITION_USD = 5.0  # each position is ~$5 by design


def snapshot() -> dict | None:
    """
    Compute portfolio USD snapshot and persist to daily_stats.

    Returns dict: {wallet_usd, position_usd, total_usd, eth_price, active_count}
    Returns None on RPC error (non-critical — agent continues).
    """
    try:
        eth_bal   = executor.get_eth_balance()
        eth_price = executor.get_eth_usd_price()
        usdc_bal  = executor.get_token_balance(USDC_ADDR, decimals=6)
    except Exception as e:
        log.warning(f'portfolio_tracker: RPC error — skipping snapshot: {e}')
        return None

    wallet_usd = eth_bal * eth_price + usdc_bal

    active       = state.get_active()
    active_count = len(active)
    position_usd = active_count * POSITION_USD

    total_usd = wallet_usd + position_usd

    state.update_daily_portfolio(total_usd, eth_price)
    log.info(
        f'portfolio snapshot: wallet=${wallet_usd:.2f}  positions=${position_usd:.2f}'
        f'  total=${total_usd:.2f}  eth=${eth_price:.0f}  ({active_count} positions)'
    )
    return {
        'wallet_usd':   wallet_usd,
        'position_usd': position_usd,
        'total_usd':    total_usd,
        'eth_price':    eth_price,
        'active_count': active_count,
    }


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout)])
    state.init_db()
    result = snapshot()
    if result:
        print(f'\nTotal portfolio: ${result["total_usd"]:.2f}')
        print(f'  Wallet:        ${result["wallet_usd"]:.2f}')
        print(f'  Positions:     ${result["position_usd"]:.2f} ({result["active_count"]} active)')
        print(f'  ETH price:     ${result["eth_price"]:.0f}')
    else:
        print('Snapshot failed (RPC error).')
        sys.exit(1)
