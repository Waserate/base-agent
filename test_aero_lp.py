"""
test_aero_lp.py — Supply-only test for aero_lp phase (9 pools).
Flow per pool: ETH->token0, ETH->token1, addLiquidity, gauge_stake, state.add_position
Does NOT withdraw — positions held until withdraw_all.py.

Usage:
    python test_aero_lp.py
"""
import os, sys, json, logging, time
from datetime import datetime
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

os.makedirs('logs', exist_ok=True)
LOG_PATH = f'logs/test_aero_lp_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

import state, executor, swap
from swap import PriceGuardError, ConfigError, SwapExecutionError

with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as f:
    CFG = json.load(f)

PLATFORMS  = CFG['phase_aero_lp']
RESULTS    = []
USDC_ADDR  = CFG['tokens']['USDC']['address']

# Tokens with no Uniswap v3 pool — must acquire via Aerodrome stable swap from USDC
AERO_STABLE_ONLY = {
    CFG['tokens']['DOLA']['address'].lower(),
    CFG['tokens']['USDz']['address'].lower(),
}


def _rec(platform, step, status, txh='', note=''):
    icon = 'OK' if status == 'OK' else 'FAIL'
    msg  = f'[{platform:28}] {step:18} {icon}'
    if txh:  msg += f'  tx={txh[:22]}...'
    if note: msg += f'  ({note})'
    log.info(msg)
    RESULTS.append({'platform': platform, 'step': step, 'status': status})


def _lp_amounts(key):
    p = CFG['platforms'][key]
    t0_tok = CFG['tokens'].get(p['token0'], {})
    t1_tok = CFG['tokens'].get(p['token1'], {})
    amt0 = int(round(t0_tok.get('position_amount', 0.01) * 10**t0_tok.get('decimals', 18)))
    amt1 = int(round(t1_tok.get('position_amount', 0.01) * 10**t1_tok.get('decimals', 18)))
    return (
        Web3.to_checksum_address(p['token0_address']),
        Web3.to_checksum_address(p['token1_address']),
        p.get('stable', False),
        amt0, amt1,
        Web3.to_checksum_address(p['pool_address']),
        Web3.to_checksum_address(p['gauge_address']),
    )


def _acquire_token(key, sym, addr, amt):
    """ETH -> token. WETH=wrap, Aero-stable-only=USDC->token via Aerodrome, else DEX swap."""
    if amt == 0:
        _rec(key, f'SKIP-{sym}', 'OK', note='ratio=0%')
        return True
    if sym == 'WETH':
        try:
            swap.wrap_eth(amt)
            _rec(key, f'WRAP->{sym}', 'OK')
            return True
        except Exception as e:
            _rec(key, f'WRAP->{sym}', 'FAIL', note=str(e))
            return False

    if addr.lower() in AERO_STABLE_ONLY:
        # No Uniswap v3 pool — acquire via ETH->USDC (Uniswap) then USDC->token (Aerodrome sAMM)
        usdc_amt = int(amt // 10**12)  # 18-dec -> 6-dec (1:1 stable peg)
        try:
            txh1 = swap.attempt_swap(swap.swap_eth_to_token, USDC_ADDR, usdc_amt)
            _rec(key, f'SWAP->USDC_for_{sym}', 'OK', txh1 or '')
        except (PriceGuardError, ConfigError, SwapExecutionError) as e:
            _rec(key, f'SWAP->USDC_for_{sym}', 'FAIL', note=str(e))
            return False
        try:
            txh2 = executor.aerodrome_swap_stable(USDC_ADDR, addr, usdc_amt)
            _rec(key, f'AERO_SWAP->{sym}', 'OK', txh2)
            return True
        except Exception as e:
            _rec(key, f'AERO_SWAP->{sym}', 'FAIL', note=str(e))
            return False

    try:
        txh = swap.attempt_swap(swap.swap_eth_to_token, addr, amt)
        _rec(key, f'SWAP->{sym}', 'OK', txh or '')
        return True
    except (PriceGuardError, ConfigError) as e:
        _rec(key, f'SWAP->{sym}', 'FAIL', note=f'price guard: {e}')
        return False
    except SwapExecutionError as e:
        _rec(key, f'SWAP->{sym}', 'FAIL', note=str(e))
        return False


def supply_aero_lp(key):
    p         = CFG['platforms'][key]
    pool_addr = Web3.to_checksum_address(p['pool_address'])
    gauge_addr = Web3.to_checksum_address(p['gauge_address'])
    t0_sym    = p['token0']
    t1_sym    = p['token1']

    # Dynamic amounts from pool ratio — $10 budget split proportionally
    t0_addr, t1_addr, stable, amt0, amt1 = executor.get_aero_lp_deposit_amounts(
        p, CFG['tokens'], budget_usd=5.0
    )

    log.info(f'\n--- {key} | {t0_sym}/{t1_sym} | stable={stable} ---')

    # Acquire tokens: non-WETH first so DEX swap residual-unwrap doesn't wipe wrapped WETH
    pairs = [(t0_sym, t0_addr, amt0), (t1_sym, t1_addr, amt1)]
    pairs.sort(key=lambda x: 1 if x[0] == 'WETH' else 0)
    for sym, addr, amt in pairs:
        if not _acquire_token(key, sym, addr, amt):
            return False

    # Step 3: addLiquidity using actual wallet balances (exactOutput swaps give exact amounts)
    try:
        txh_lp, lp_recv = executor.aerodrome_add_liquidity(
            t0_addr, t1_addr, stable, amt0, amt1
        )
        _rec(key, 'ADD_LIQUIDITY', 'OK', txh_lp, f'LP={lp_recv}')
    except Exception as e:
        _rec(key, 'ADD_LIQUIDITY', 'FAIL', note=str(e))
        return False

    if lp_recv == 0:
        _rec(key, 'ADD_LIQUIDITY', 'FAIL', note='received 0 LP after sleep(4)')
        return False

    # Step 4: stake LP in gauge
    try:
        txh_stake = executor.aerodrome_gauge_stake(pool_addr, gauge_addr, lp_recv)
        _rec(key, 'GAUGE_STAKE', 'OK', txh_stake, f'LP={lp_recv}')
    except Exception as e:
        _rec(key, 'GAUGE_STAKE', 'FAIL', note=str(e))
        return False

    # Step 5: record position
    state.add_position(key, 'LP', lp_recv, 30, txh_stake)
    _rec(key, 'STATE', 'OK', note=f'LP={lp_recv} expiry +30d')
    return True


if __name__ == '__main__':
    state.init_db()
    bal = executor.get_eth_balance()

    log.info('=' * 65)
    log.info('AERO LP -- SUPPLY TEST (9 pools)')
    log.info(f'Wallet   : {executor.WALLET}')
    log.info(f'ETH bal  : {bal:.5f}')
    log.info(f'Platforms: {PLATFORMS}')
    log.info('=' * 65)

    if bal < 0.03:
        log.error(f'ETH too low ({bal:.5f}). Need >=0.03 ETH for 9 pools.')
        sys.exit(1)

    for key in PLATFORMS:
        supply_aero_lp(key)
        time.sleep(2)  # breathe between pools to avoid RPC rate limit

    log.info('\n' + '=' * 65)
    log.info('SUMMARY')
    log.info('-' * 65)
    seen = {}
    for r in RESULTS:
        seen.setdefault(r['platform'], []).append(r)
    passed = failed = 0
    for plat, steps in seen.items():
        ok = all(s['status'] == 'OK' for s in steps)
        log.info(f'  [{"PASS" if ok else "FAIL"}] {plat}')
        if ok:  passed += 1
        else:   failed += 1
    log.info('-' * 65)
    log.info(f'  {passed}/{passed+failed} PASS  |  Log: {LOG_PATH}')
    log.info('=' * 65)
