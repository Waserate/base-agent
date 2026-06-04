"""
roundtrip_lp.py — Full deposit+withdraw cycle for ONE aero_lp pool.
Usage: python roundtrip_lp.py <pool_key>
       python roundtrip_lp.py aero_lp_weth_usdc
"""
import sys, json, os, logging, time
from datetime import datetime
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


# -- Token acquisition ----------------------------------------------------------

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


# -- Token → ETH ---------------------------------------------------------------

def _to_eth(sym, addr):
    c = executor.w3.eth.contract(
        address=Web3.to_checksum_address(addr), abi=executor.ERC20_ABI
    )
    bal = c.functions.balanceOf(executor.WALLET).call()
    if bal == 0:
        return
    if sym == 'WETH':
        swap.unwrap_all_weth()
        log.info(f'  UNWRAP WETH OK')
    elif addr.lower() in AERO_STABLE_ONLY:
        executor.aerodrome_swap_stable(addr, USDC_ADDR, bal)
        log.info(f'  AERO {sym}->USDC OK')
        time.sleep(4)
        usdc_c = executor.w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDR), abi=executor.ERC20_ABI
        )
        usdc_bal = usdc_c.functions.balanceOf(executor.WALLET).call()
        if usdc_bal > 0:
            swap.attempt_swap(swap.swap_token_to_eth, USDC_ADDR, usdc_bal)
            log.info(f'  USDC->ETH OK')
    else:
        swap.attempt_swap(swap.swap_token_to_eth, addr, bal)
        log.info(f'  SWAP {sym}->ETH OK')


# -- Main round trip ------------------------------------------------------------

def roundtrip(pool_key):
    # Do NOT reset nonce per pool — nonce accumulates from the single init at script start
    p = CFG['platforms'][pool_key]
    t0_sym, t1_sym = p['token0'], p['token1']
    pool_addr  = Web3.to_checksum_address(p['pool_address'])
    gauge_addr = Web3.to_checksum_address(p['gauge_address'])
    stable = p.get('stable', False)

    log.info(f'\n{"="*60}')
    log.info(f'ROUNDTRIP: {pool_key}')
    log.info(f'{"="*60}')

    eth_before = executor.get_eth_balance()
    log.info(f'ETH before: {eth_before:.5f}')

    # -- DEPOSIT ---------------------------------------------------------------
    t0_addr, t1_addr, _, amt0, amt1 = executor.get_aero_lp_deposit_amounts(
        p, CFG['tokens'], budget_usd=5.0
    )
    log.info(f'Ratio: ${amt0/10**CFG["tokens"][t0_sym]["decimals"]*executor.get_token_usd_price(t0_sym):.2f} {t0_sym}'
             f' + ${amt1/10**CFG["tokens"][t1_sym]["decimals"]*executor.get_token_usd_price(t1_sym):.2f} {t1_sym}')

    # Acquire tokens: non-WETH first
    pairs = [(t0_sym, t0_addr, amt0), (t1_sym, t1_addr, amt1)]
    pairs.sort(key=lambda x: 1 if x[0] == 'WETH' else 0)
    for sym, addr, amt in pairs:
        if not _acquire(sym, addr, amt):
            log.error('DEPOSIT FAILED at token acquisition')
            return False

    # Add liquidity
    lp_c = executor.w3.eth.contract(address=pool_addr, abi=executor.ERC20_ABI)
    lp_before = lp_c.functions.balanceOf(executor.WALLET).call()
    try:
        txh_lp, lp_recv = executor.aerodrome_add_liquidity(t0_addr, t1_addr, stable, amt0, amt1)
        log.info(f'  ADD_LIQUIDITY OK  LP={lp_recv}  tx={txh_lp[:22]}...')
    except Exception as e:
        log.error(f'  ADD_LIQUIDITY FAIL: {e}')
        return False

    if lp_recv == 0:
        log.error('  No LP received')
        return False

    # Stake in gauge
    try:
        executor.aerodrome_gauge_stake(pool_addr, gauge_addr, lp_recv)
        log.info(f'  GAUGE_STAKE OK')
    except Exception as e:
        log.error(f'  GAUGE_STAKE FAIL: {e}')
        return False

    # -- WITHDRAW --------------------------------------------------------------
    time.sleep(4)  # let public RPC reflect stake before reading

    # Claim rewards (best-effort)
    try:
        executor.aerodrome_gauge_claim(gauge_addr)
        log.info(f'  GAUGE_CLAIM OK')
    except Exception as e:
        log.warning(f'  GAUGE_CLAIM skipped: {e}')

    # Unstake
    gauge_c = executor.w3.eth.contract(address=gauge_addr, abi=executor.GAUGE_ABI)
    staked = gauge_c.functions.balanceOf(executor.WALLET).call()
    if staked == 0:
        log.error('  Nothing staked')
        return False
    try:
        executor.aerodrome_gauge_unstake(gauge_addr, staked)
        log.info(f'  GAUGE_UNSTAKE OK')
    except Exception as e:
        log.error(f'  GAUGE_UNSTAKE FAIL: {e}')
        return False

    time.sleep(4)

    # Remove liquidity
    lp_bal = lp_c.functions.balanceOf(executor.WALLET).call()
    if lp_bal == 0:
        log.error('  No LP after unstake')
        return False
    try:
        executor.aerodrome_remove_liquidity(t0_addr, t1_addr, stable, lp_bal)
        log.info(f'  REMOVE_LIQUIDITY OK')
    except Exception as e:
        log.error(f'  REMOVE_LIQUIDITY FAIL: {e}')
        return False

    time.sleep(4)  # let RPC reflect token balances after removeLiquidity

    # Convert tokens → ETH
    for sym, addr in [(t0_sym, p['token0_address']), (t1_sym, p['token1_address'])]:
        try:
            _to_eth(sym, addr)
        except Exception as e:
            log.warning(f'  to_eth {sym} error: {e}')

    # -- RESULT ----------------------------------------------------------------
    eth_after = executor.get_eth_balance()
    net = eth_after - eth_before
    eth_price = executor.get_token_usd_price('WETH')

    log.info(f'\n{"-"*60}')
    log.info(f'RESULT: {pool_key}')
    log.info(f'  LP received : {lp_recv}')
    log.info(f'  ETH before  : {eth_before:.5f}  (${eth_before*eth_price:.2f})')
    log.info(f'  ETH after   : {eth_after:.5f}  (${eth_after*eth_price:.2f})')
    log.info(f'  Net         : {net:+.5f} ETH  (${net*eth_price:.2f})  [gas + fees]')
    log.info(f'{"-"*60}')
    return True


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python roundtrip_lp.py <pool_key>')
        print('Pools:', list(CFG.get('phase_aero_lp', [])))
        sys.exit(1)
    state.init_db()
    # Init nonce ONCE — let it accumulate; sleep 5s for Alchemy state to settle
    executor.reset_nonce()
    roundtrip(sys.argv[1])
