"""
deposit_lp.py — Deposit + gauge stake ONE aero_lp pool. Does NOT withdraw.

Usage:
    python deposit_lp.py <pool_key>
    python deposit_lp.py aero_lp_weth_usdc

After this completes, approve withdrawal, then:
    python withdraw_all.py
"""
import sys, json, os, logging, time
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

import state, executor, swap
from swap import PriceGuardError, ConfigError, SwapExecutionError

with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as f:
    CFG = json.load(f)

USDC_ADDR = CFG['tokens']['USDC']['address']
AERO_STABLE_ONLY = {
    CFG['tokens']['DOLA']['address'].lower(),
    CFG['tokens']['USDz']['address'].lower(),
}


def _acquire(sym, addr, amt):
    if amt == 0:
        log.info(f'  SKIP {sym} (ratio=0%)')
        return True
    if sym == 'WETH':
        try:
            swap.wrap_eth(amt)
            log.info(f'  WRAP->{sym} OK')
            return True
        except Exception as e:
            log.error(f'  WRAP->{sym} FAIL: {e}')
            return False
    if addr.lower() in AERO_STABLE_ONLY:
        usdc_amt = int(amt // 10**12)
        try:
            swap.attempt_swap(swap.swap_eth_to_token, USDC_ADDR, usdc_amt)
            log.info(f'  SWAP->USDC for {sym} OK')
        except Exception as e:
            log.error(f'  SWAP->USDC for {sym} FAIL: {e}')
            return False
        try:
            executor.aerodrome_swap_stable(USDC_ADDR, addr, usdc_amt)
            log.info(f'  AERO_SWAP->{sym} OK')
            return True
        except Exception as e:
            log.error(f'  AERO_SWAP->{sym} FAIL: {e}')
            return False
    try:
        swap.attempt_swap(swap.swap_eth_to_token, addr, amt)
        log.info(f'  SWAP->{sym} OK')
        return True
    except (PriceGuardError, ConfigError, SwapExecutionError) as e:
        log.error(f'  SWAP->{sym} FAIL: {e}')
        return False


def deposit(pool_key):
    p = CFG['platforms'][pool_key]
    t0_sym, t1_sym = p['token0'], p['token1']
    pool_addr  = Web3.to_checksum_address(p['pool_address'])
    gauge_addr = Web3.to_checksum_address(p['gauge_address'])
    stable     = p.get('stable', False)

    log.info(f'\n{"="*60}')
    log.info(f'DEPOSIT LP: {pool_key}')
    log.info(f'{"="*60}')

    eth_before = executor.get_eth_balance()
    log.info(f'ETH before: {eth_before:.5f}')

    # Calculate amounts based on live pool ratio
    t0_addr, t1_addr, _, amt0, amt1 = executor.get_aero_lp_deposit_amounts(
        p, CFG['tokens'], budget_usd=5.0
    )
    t0_price = executor.get_token_usd_price(t0_sym)
    t1_price = executor.get_token_usd_price(t1_sym)
    t0_dec   = CFG['tokens'][t0_sym]['decimals']
    t1_dec   = CFG['tokens'][t1_sym]['decimals']
    log.info(f'Target: ${amt0/10**t0_dec*t0_price:.2f} {t0_sym}'
             f' + ${amt1/10**t1_dec*t1_price:.2f} {t1_sym}')

    # Acquire tokens — non-WETH first to avoid WETH ordering bug
    pairs = [(t0_sym, t0_addr, amt0), (t1_sym, t1_addr, amt1)]
    pairs.sort(key=lambda x: 1 if x[0] == 'WETH' else 0)
    for sym, addr, amt in pairs:
        if not _acquire(sym, addr, amt):
            log.error('DEPOSIT FAILED at token acquisition')
            return False

    # Add liquidity
    try:
        txh_lp, lp_recv = executor.aerodrome_add_liquidity(t0_addr, t1_addr, stable, amt0, amt1)
        log.info(f'  ADD_LIQUIDITY OK  LP={lp_recv}  tx={txh_lp[:22]}...')
    except Exception as e:
        log.error(f'  ADD_LIQUIDITY FAIL: {e}')
        return False

    if lp_recv == 0:
        log.error('  No LP received — check token amounts / pool ratio')
        return False

    # Stake LP in gauge
    try:
        executor.aerodrome_gauge_stake(pool_addr, gauge_addr, lp_recv)
        log.info(f'  GAUGE_STAKE OK')
    except Exception as e:
        log.error(f'  GAUGE_STAKE FAIL: {e}')
        return False

    # Wait for RPC to reflect stake, then read gauge balance on-chain
    time.sleep(4)
    gauge_c   = executor.w3.eth.contract(address=gauge_addr, abi=executor.GAUGE_ABI)
    gauge_bal = gauge_c.functions.balanceOf(executor.WALLET).call()

    # Save to state.db so withdraw_all.py can find and process it
    state.add_position(
        platform=pool_key,
        token='LP',
        amount_wei=lp_recv,
        expiry_days=7,
        tx_hash=txh_lp,
    )

    eth_after = executor.get_eth_balance()
    eth_used  = eth_before - eth_after
    eth_price = executor.get_token_usd_price('WETH')

    log.info(f'\n{"-"*60}')
    log.info(f'DEPOSIT COMPLETE: {pool_key}')
    log.info(f'  LP received  : {lp_recv}')
    log.info(f'  Gauge balance: {gauge_bal}  {"OK" if gauge_bal > 0 else "WARNING: 0 - check manually"}')
    log.info(f'  ETH used     : {eth_used:.5f}  (~${eth_used*eth_price:.2f})')
    log.info(f'  ETH remain   : {eth_after:.5f}  (~${eth_after*eth_price:.2f})')
    log.info(f'  DB saved     : YES  platform={pool_key}  token=LP')
    log.info(f'{"-"*60}')
    log.info(f'>> WAITING FOR YOUR WITHDRAWAL APPROVAL.')
    log.info(f'>> When ready: python withdraw_all.py')
    return True


if __name__ == '__main__':
    POOLS = [k for k in CFG.get('platforms', {}) if k.startswith('aero_lp_')]
    if len(sys.argv) < 2:
        print('Usage: python deposit_lp.py <pool_key>')
        print('Available aero_lp pools:')
        for p in POOLS:
            print(f'  {p}')
        sys.exit(1)
    pool_key = sys.argv[1]
    if pool_key not in CFG['platforms']:
        print(f'ERROR: unknown pool "{pool_key}"')
        print('Available:', POOLS)
        sys.exit(1)
    state.init_db()
    executor.reset_nonce()
    success = deposit(pool_key)
    sys.exit(0 if success else 1)
