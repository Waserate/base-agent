"""
test_uni_lp.py — DRY_RUN test for all 10 Uniswap v3 LP pools.

Tests:
  1. Pool fee query on-chain
  2. Price + budget split calculation
  3. Full mint flow (DRY_RUN — no TX sent, no tokens needed)

Usage:
    DRY_RUN=true python test_uni_lp.py
    DRY_RUN=true python test_uni_lp.py uni_lp_weth_usdc_3000   # single pool
"""

import os, sys, json, time, logging
from dotenv import load_dotenv
load_dotenv()

import executor, uni_lp
from swap import PriceGuardError, ConfigError, SwapExecutionError
import swap

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

os.environ.setdefault('DRY_RUN', 'true')

cfg     = executor._load_cfg()
pools   = cfg['phase_uni_lp']

if len(sys.argv) > 1:
    pools = [sys.argv[1]]

print('=' * 72)
print(f'UNI_LP DRY_RUN TEST  —  {len(pools)} pools')
print(f'Wallet : {executor.WALLET}')
print(f'DRY_RUN: {executor.DRY_RUN}')
print('=' * 72)

results = []

for pool_key in pools:
    p = cfg['platforms'][pool_key]
    print(f'\n--- {pool_key} ---')
    try:
        # 1. Fee
        fee   = uni_lp._get_pool_fee(p['pool_address'])
        ticks = uni_lp.FULL_RANGE_TICKS.get(fee, (-887270, 887270))
        print(f'  fee={fee}  ticks={ticks}')

        # 2. Price + budget split
        t0_sym = p['token0']
        t1_sym = p['token1']
        p0 = executor.get_token_usd_price(t0_sym)
        p1 = executor.get_token_usd_price(t1_sym)
        t0_dec = cfg['tokens'].get(t0_sym, {}).get('decimals', 18)
        t1_dec = cfg['tokens'].get(t1_sym, {}).get('decimals', 18)
        half = uni_lp.UNI_LP_BUDGET_USD / 2
        amt0 = int(half / p0 * 10**t0_dec)
        amt1 = int(half / p1 * 10**t1_dec)
        print(f'  ${p0:.4f} {t0_sym}: {amt0/10**t0_dec:.6f} ({half:.2f} USD)')
        print(f'  ${p1:.4f} {t1_sym}: {amt1/10**t1_dec:.6f} ({half:.2f} USD)')

        # 3. Dry mint
        executor.reset_nonce()
        token_id, txh = uni_lp.mint_uni_lp(pool_key)
        print(f'  MINT DRY OK  tokenId={token_id}  tx={txh[:18]}...')
        results.append((pool_key, 'PASS', None))

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f'  FAIL: {e}')
        results.append((pool_key, 'FAIL', str(e)[:80]))

    if len(pools) > 1:
        time.sleep(3)  # avoid 429 on public RPC between pools

print('\n' + '=' * 72)
print('SUMMARY')
ok  = sum(1 for _, s, _ in results if s == 'PASS')
bad = len(results) - ok
for pool_key, status, err in results:
    note = f'  << {err}' if err else ''
    print(f'  {status:4}  {pool_key}{note}')
print(f'\n  {ok}/{len(results)} PASS  {bad} FAIL')
print('=' * 72)
