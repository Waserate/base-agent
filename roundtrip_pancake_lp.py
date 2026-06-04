"""
roundtrip_pancake_lp.py — Live test: mint + pause + close for PancakeSwap v3 LP pools.

Flow per pool:
  1. Acquire token0 + token1 via swap (50:50 USD, $5 total)
  2. Mint full-range position -> get tokenId
  3. PAUSE -- show tokenId/tx, wait user Enter
  4. decreaseLiquidity + collect -> tokens back in wallet
  5. Convert tokens -> ETH, report net ETH delta
  6. Auto-advance to next pool (if running all)

Usage:
    python roundtrip_pancake_lp.py                               # all 13 pools
    python roundtrip_pancake_lp.py pancake_lp_weth_usdc_100      # single pool
"""

import sys, json, os, time, logging
from dotenv import load_dotenv
from web3 import Web3
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    datefmt='%H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

import state, executor, swap, pancake_lp
from swap import PriceGuardError, ConfigError, SwapExecutionError

with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as f:
    CFG = json.load(f)


# -- Token acquisition ---------------------------------------------------------

def _acquire(sym, addr, amt):
    if sym == 'WETH':
        try:
            swap.wrap_eth(amt)
            log.info(f'  WRAP -> WETH OK')
            return True
        except Exception as e:
            log.error(f'  WRAP FAIL: {e}')
            return False
    try:
        swap.attempt_swap(swap.swap_eth_to_token, addr, amt)
        log.info(f'  SWAP -> {sym} OK')
        return True
    except (PriceGuardError, ConfigError, SwapExecutionError) as e:
        log.error(f'  SWAP -> {sym} FAIL: {e}')
        return False


# -- Token -> ETH --------------------------------------------------------------

def _to_eth(sym, addr):
    c = executor.w3.eth.contract(
        address=Web3.to_checksum_address(addr), abi=executor.ERC20_ABI
    )
    bal = c.functions.balanceOf(executor.WALLET).call()
    if bal == 0:
        log.info(f'  {sym} balance=0, skip')
        return
    if sym == 'WETH':
        swap.unwrap_all_weth()
        log.info(f'  UNWRAP WETH OK')
    else:
        swap.attempt_swap(swap.swap_token_to_eth, addr, bal)
        log.info(f'  SWAP {sym} -> ETH OK')


# -- Roundtrip -----------------------------------------------------------------

def roundtrip(pool_key: str) -> bool:
    p      = CFG['platforms'][pool_key]
    t0_sym = p['token0']
    t1_sym = p['token1']
    t0_addr = Web3.to_checksum_address(p['token0_address'])
    t1_addr = Web3.to_checksum_address(p['token1_address'])

    log.info(f'\n{"=" * 60}')
    log.info(f'POOL: {pool_key}')
    log.info(f'{"=" * 60}')

    eth_before = executor.get_eth_balance()
    log.info(f'ETH before: {eth_before:.5f}')

    # -- STEP 1: Calculate amounts ---------------------------------------------
    fee   = pancake_lp._get_pool_fee(p['pool_address'])
    ticks = pancake_lp.FULL_RANGE_TICKS.get(fee, (-887250, 887250))
    p0    = executor.get_token_usd_price(t0_sym)
    p1    = executor.get_token_usd_price(t1_sym)
    t0_dec = CFG['tokens'].get(t0_sym, {}).get('decimals', 18)
    t1_dec = CFG['tokens'].get(t1_sym, {}).get('decimals', 18)
    half   = pancake_lp.PANCAKE_LP_BUDGET_USD / 2
    amt0   = int(half / p0 * 10**t0_dec)
    amt1   = int(half / p1 * 10**t1_dec)

    log.info(f'fee={fee}  ticks={ticks}')
    log.info(f'Budget: ${half:.2f} {t0_sym} ({amt0/10**t0_dec:.6f}) + ${half:.2f} {t1_sym} ({amt1/10**t1_dec:.6f})')

    # -- STEP 2: Acquire tokens (non-WETH first) -------------------------------
    log.info('--- Acquiring tokens ---')
    pairs = [(t0_sym, t0_addr, amt0), (t1_sym, t1_addr, amt1)]
    pairs.sort(key=lambda x: 1 if x[0] == 'WETH' else 0)
    for sym, addr, amt in pairs:
        if not _acquire(sym, addr, amt):
            log.error('DEPOSIT FAILED at token acquisition')
            return False

    # -- STEP 3: Mint LP position ----------------------------------------------
    log.info('--- Minting LP position ---')
    try:
        token_id, txh = pancake_lp.mint_pancake_lp(pool_key)
        log.info(f'  MINT OK  tokenId={token_id}  tx={txh[:22]}...')
    except Exception as e:
        log.error(f'  MINT FAIL: {e}')
        return False

    # -- STEP 4: PAUSE -- inspect on-chain before withdrawing -----------------
    log.info(f'\n{"*" * 60}')
    log.info(f'DEPOSIT DONE -- pool={pool_key}')
    log.info(f'  tokenId : {token_id}')
    log.info(f'  tx      : {txh}')
    log.info(f'  Check on-chain then press Enter to withdraw (Ctrl+C to halt)')
    log.info(f'{"*" * 60}')
    try:
        input('\n>>> Press Enter to WITHDRAW ... ')
    except KeyboardInterrupt:
        log.info('\nHalted by user -- position left open (tokenId saved above)')
        return False

    # -- STEP 5: Close position ------------------------------------------------
    log.info('--- Closing LP position ---')
    try:
        txh_close = pancake_lp.close_pancake_lp(token_id)
        log.info(f'  CLOSE OK  tx={txh_close[:22]}...')
    except Exception as e:
        log.error(f'  CLOSE FAIL: {e}')
        log.warning(f'  tokenId={token_id} still open -- withdraw manually')
        return False

    time.sleep(4)

    # -- STEP 6: Convert tokens -> ETH -----------------------------------------
    log.info('--- Converting tokens to ETH ---')
    for sym, addr in [(t0_sym, p['token0_address']), (t1_sym, p['token1_address'])]:
        try:
            _to_eth(sym, addr)
        except Exception as e:
            log.warning(f'  to_eth {sym} error (run sweep_tokens.py to recover): {e}')

    # -- STEP 7: Result --------------------------------------------------------
    eth_after = executor.get_eth_balance()
    net       = eth_after - eth_before
    eth_price = executor.get_token_usd_price('WETH')

    log.info(f'\n{"-" * 60}')
    log.info(f'RESULT: {pool_key}')
    log.info(f'  tokenId    : {token_id}')
    log.info(f'  ETH before : {eth_before:.5f}  (${eth_before * eth_price:.2f})')
    log.info(f'  ETH after  : {eth_after:.5f}  (${eth_after * eth_price:.2f})')
    log.info(f'  Net        : {net:+.5f} ETH  (${net * eth_price:.2f})  [gas cost]')
    log.info(f'{"-" * 60}')
    return True


# -- Main ----------------------------------------------------------------------

if __name__ == '__main__':
    state.init_db()
    executor.reset_nonce()

    pools = CFG.get('phase_pancake_lp', [])
    if len(sys.argv) > 1:
        pools = [sys.argv[1]]

    print(f'\nPANCAKE_LP ROUNDTRIP TEST -- {len(pools)} pools')
    print(f'Wallet: {executor.WALLET}\n')

    results = []
    for i, pool_key in enumerate(pools, 1):
        print(f'\n[{i}/{len(pools)}] {pool_key}')
        executor.reset_nonce()
        ok = roundtrip(pool_key)
        results.append((pool_key, 'PASS' if ok else 'FAIL'))

        if not ok:
            cont = input('\nPool failed. Continue to next? (y/n): ').strip().lower()
            if cont != 'y':
                break

    print(f'\n{"=" * 60}')
    print('FINAL SUMMARY')
    for key, status in results:
        print(f'  {status}  {key}')
    ok_count = sum(1 for _, s in results if s == 'PASS')
    print(f'\n  {ok_count}/{len(results)} PASS')
    print('=' * 60)
