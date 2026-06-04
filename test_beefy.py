"""
test_beefy.py — Supply-only test for Phase 5 Beefy platforms.
Tests beefy_single (3) and beefy_lp (2) sequentially.
Does NOT withdraw — positions held until manual withdraw.

Usage:
    python test_beefy.py
"""
import os, sys, json, logging, time
from datetime import datetime
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

os.makedirs('logs', exist_ok=True)
LOG_PATH = f'logs/test_beefy_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'

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

USDC_ADDR = CFG['tokens']['USDC']['address']
PLATFORMS = CFG['phase5']
RESULTS   = []

def _rec(platform, step, status, txh='', note=''):
    icon = 'OK' if status == 'OK' else 'FAIL'
    msg  = f'[{platform:22}] {step:14} {icon}'
    if txh:  msg += f'  tx={txh[:22]}...'
    if note: msg += f'  ({note})'
    log.info(msg)
    RESULTS.append({'platform': platform, 'step': step, 'status': status})

def _tok_addr(key):
    return CFG['platforms'][key].get('token_address', USDC_ADDR)

def _amount(key):
    p   = CFG['platforms'][key]
    tok = CFG['tokens'].get(p.get('token', ''), {})
    return int(round(tok.get('position_amount', 0.01) * 10**tok.get('decimals', 18)))

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
        Web3.to_checksum_address(p['lp_address']),
    )


def supply_single(key):
    p       = CFG['platforms'][key]
    token   = p['token']
    tok_addr = _tok_addr(key)
    amt     = _amount(key)

    log.info(f'\n--- {key} | type=beefy_single | token={token} | amount={amt} ---')

    # ETH -> token
    if token == 'WETH':
        try:
            swap.wrap_eth(amt)
            _rec(key, 'WRAP', 'OK')
        except Exception as e:
            _rec(key, 'WRAP', 'FAIL', note=str(e))
            return False
    else:
        try:
            txh = swap.attempt_swap(swap.swap_eth_to_token, tok_addr, amt)
            _rec(key, f'SWAP->{token}', 'OK', txh or '')
        except (PriceGuardError, ConfigError) as e:
            _rec(key, f'SWAP->{token}', 'FAIL', note=f'price guard: {e}')
            return False
        except SwapExecutionError as e:
            _rec(key, f'SWAP->{token}', 'FAIL', note=str(e))
            return False

    # beefy deposit
    try:
        txh = executor.beefy_deposit(p['address'], tok_addr, amt)
        _rec(key, 'BEEFY_DEPOSIT', 'OK', txh)
    except Exception as e:
        _rec(key, 'BEEFY_DEPOSIT', 'FAIL', note=str(e))
        return False

    state.add_position(key, token, amt, 30, txh)
    _rec(key, 'STATE', 'OK', note='expiry +30d')
    return True


def supply_lp(key):
    p = CFG['platforms'][key]
    t0_addr, t1_addr, stable, amt0, amt1, lp_addr = _lp_amounts(key)
    t0_sym = p['token0']
    t1_sym = p['token1']

    log.info(f'\n--- {key} | type=beefy_lp | {t0_sym}+{t1_sym} ---')

    # Step 1: ETH -> token0
    if t0_sym == 'WETH':
        try:
            swap.wrap_eth(amt0)
            _rec(key, f'WRAP->{t0_sym}', 'OK')
        except Exception as e:
            _rec(key, f'WRAP->{t0_sym}', 'FAIL', note=str(e))
            return False
    else:
        try:
            txh = swap.attempt_swap(swap.swap_eth_to_token, t0_addr, amt0)
            _rec(key, f'SWAP->{t0_sym}', 'OK', txh or '')
        except (PriceGuardError, ConfigError, SwapExecutionError) as e:
            _rec(key, f'SWAP->{t0_sym}', 'FAIL', note=str(e))
            return False

    # Step 2: ETH -> token1
    if t1_sym == 'WETH':
        try:
            swap.wrap_eth(amt1)
            _rec(key, f'WRAP->{t1_sym}', 'OK')
        except Exception as e:
            _rec(key, f'WRAP->{t1_sym}', 'FAIL', note=str(e))
            return False
    else:
        try:
            txh = swap.attempt_swap(swap.swap_eth_to_token, t1_addr, amt1)
            _rec(key, f'SWAP->{t1_sym}', 'OK', txh or '')
        except (PriceGuardError, ConfigError, SwapExecutionError) as e:
            _rec(key, f'SWAP->{t1_sym}', 'FAIL', note=str(e))
            return False

    # Step 3: addLiquidity Aerodrome
    lp_c = executor.w3.eth.contract(
        address=lp_addr, abi=executor.ERC20_ABI
    )
    lp_before = lp_c.functions.balanceOf(executor.WALLET).call()
    try:
        txh_lp, lp_recv = executor.aerodrome_add_liquidity(
            t0_addr, t1_addr, stable, amt0, amt1
        )
        _rec(key, 'ADD_LIQUIDITY', 'OK', txh_lp, f'LP={lp_recv}')
    except Exception as e:
        _rec(key, 'ADD_LIQUIDITY', 'FAIL', note=str(e))
        return False

    if lp_recv == 0:
        _rec(key, 'ADD_LIQUIDITY', 'FAIL', note='received 0 LP')
        return False

    # Step 4: Beefy deposit LP
    try:
        txh = executor.beefy_deposit(p['address'], lp_addr, lp_recv)
        _rec(key, 'BEEFY_DEPOSIT', 'OK', txh)
    except Exception as e:
        _rec(key, 'BEEFY_DEPOSIT', 'FAIL', note=str(e))
        return False

    state.add_position(key, 'LP', lp_recv, 30, txh)
    _rec(key, 'STATE', 'OK', note=f'LP={lp_recv} expiry +30d')
    return True


if __name__ == '__main__':
    state.init_db()
    bal = executor.get_eth_balance()

    log.info('=' * 60)
    log.info('BEEFY PHASE 5 -- SUPPLY TEST')
    log.info(f'Wallet   : {executor.WALLET}')
    log.info(f'ETH bal  : {bal:.5f}')
    log.info(f'Platforms: {PLATFORMS}')
    log.info('=' * 60)

    if bal < 0.04:
        log.error(f'ETH too low ({bal:.5f}). Need >=0.04 ETH.')
        sys.exit(1)

    for key in PLATFORMS:
        p = CFG['platforms'][key]
        if p['type'] == 'beefy_single':
            supply_single(key)
        elif p['type'] == 'beefy_lp':
            supply_lp(key)

    log.info('\n' + '=' * 60)
    log.info('SUMMARY')
    log.info('-' * 60)
    seen = {}
    for r in RESULTS:
        seen.setdefault(r['platform'], []).append(r)
    passed = failed = 0
    for plat, steps in seen.items():
        ok = all(s['status'] == 'OK' for s in steps)
        log.info(f'  [{"PASS" if ok else "FAIL"}] {plat}')
        if ok: passed += 1
        else:  failed += 1
    log.info('-' * 60)
    log.info(f'  {passed}/{passed+failed} PASS  |  Log: {LOG_PATH}')
    log.info('=' * 60)
