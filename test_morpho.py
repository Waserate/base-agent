"""
test_morpho.py — Supply-only test for Phase 4 Morpho platforms.
Tests morpho_usdc, morpho_eth, morpho_eurc, morpho_cbbtc sequentially.
Does NOT withdraw — positions held until manual withdraw.

Usage:
    python test_morpho.py
"""
import os, sys, json, logging
from datetime import datetime
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

os.makedirs('logs', exist_ok=True)
LOG_PATH = f'logs/test_morpho_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'

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

USDC_ADDR  = CFG['tokens']['USDC']['address']
PLATFORMS  = CFG['phase4']
RESULTS    = []

def _tok_addr(key):
    return CFG['platforms'][key].get('token_address', USDC_ADDR)

def _amount(key):
    p   = CFG['platforms'][key]
    tok = CFG['tokens'].get(p['token'], {})
    return int(round(tok.get('position_amount', 0.01) * 10**tok.get('decimals', 18)))

def _rec(platform, step, status, txh='', note=''):
    icon = 'OK' if status == 'OK' else 'FAIL'
    msg  = f'[{platform:18}] {step:12} {icon}'
    if txh:  msg += f'  tx={txh[:20]}...'
    if note: msg += f'  ({note})'
    log.info(msg)
    RESULTS.append({'platform': platform, 'step': step, 'status': status})

def supply(key):
    p        = CFG['platforms'][key]
    token    = p['token']
    tok_addr = _tok_addr(key)
    amt      = _amount(key)

    log.info(f'\n--- {key} | token={token} | amount={amt} ---')

    # ETH -> token
    if token == 'WETH':
        try:
            swap.wrap_eth(amt)
            _rec(key, 'WRAP', 'OK')
        except Exception as e:
            _rec(key, 'WRAP', 'FAIL', note=str(e))
            return False
    elif token != 'ETH':
        try:
            txh = swap.attempt_swap(swap.swap_eth_to_token, tok_addr, amt)
            _rec(key, f'SWAP->{token}', 'OK', txh or '')
        except (PriceGuardError, ConfigError) as e:
            _rec(key, f'SWAP->{token}', 'FAIL', note=f'price guard: {e}')
            return False
        except SwapExecutionError as e:
            _rec(key, f'SWAP->{token}', 'FAIL', note=str(e))
            return False

    # deposit (all phase4 = erc4626)
    try:
        txh = executor.erc4626_deposit(p['address'], tok_addr, amt)
        _rec(key, 'DEPOSIT', 'OK', txh)
    except Exception as e:
        _rec(key, 'DEPOSIT', 'FAIL', note=str(e))
        return False

    # record in state.db
    state.add_position(key, token, amt, 30, txh)
    _rec(key, 'STATE', 'OK', note='expiry +30d, manual withdraw')
    return True


if __name__ == '__main__':
    state.init_db()
    w3  = executor.w3
    bal = float(Web3.from_wei(w3.eth.get_balance(executor.WALLET), 'ether'))

    log.info('=' * 55)
    log.info('MORPHO PHASE 4 — SUPPLY TEST')
    log.info(f'Wallet   : {executor.WALLET}')
    log.info(f'ETH bal  : {bal:.5f}')
    log.info(f'Platforms: {PLATFORMS}')
    log.info('=' * 55)

    if bal < 0.02:
        log.error(f'ETH too low ({bal:.5f}). Need >=0.02 ETH.')
        sys.exit(1)

    for key in PLATFORMS:
        supply(key)

    log.info('\n' + '=' * 55)
    log.info('SUMMARY')
    log.info('-' * 55)
    passed = failed = 0
    seen = {}
    for r in RESULTS:
        seen.setdefault(r['platform'], []).append(r)
    for plat, steps in seen.items():
        ok = all(s['status'] == 'OK' for s in steps)
        mark = 'PASS' if ok else 'FAIL'
        log.info(f'  [{mark}] {plat}')
        if ok: passed += 1
        else:  failed += 1
    log.info('-' * 55)
    log.info(f'  {passed}/{passed+failed} PASS  |  Log: {LOG_PATH}')
    log.info('=' * 55)
