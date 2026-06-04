"""
test_full.py — Supply-only full platform test
Tests all 9 platforms sequentially. Logs every step (swap/approve/supply).
Does NOT withdraw — positions held until manual withdraw command.

Usage:
    python test_full.py

Log file: logs/test_full.log
"""
import os, json, logging, sys
from datetime import datetime
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

os.makedirs('logs', exist_ok=True)

LOG_PATH = f'logs/test_full_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'

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

import state
import executor
import swap
from swap import PriceGuardError, ConfigError, SwapExecutionError

with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as f:
    CFG = json.load(f)

USDC_ADDR = CFG['tokens']['USDC']['address']
USDS_ADDR = CFG['tokens']['USDS']['address']

ALL_PLATFORMS = CFG['phase1'] + CFG.get('phase2', []) + CFG.get('phase3', [])

RESULTS: list[dict] = []

def _rec(platform, step, status, tx_hash='', note=''):
    icon = '✅' if status == 'OK' else '❌'
    msg  = f'[{platform:20s}] {step:10s} {icon} {status}'
    if tx_hash:
        msg += f'  tx={tx_hash[:18]}...'
    if note:
        msg += f'  ({note})'
    log.info(msg)
    RESULTS.append({'platform': platform, 'step': step, 'status': status,
                    'tx': tx_hash, 'note': note})

def _token_addr(platform_key):
    p = CFG['platforms'][platform_key]
    return p.get('token_address', USDC_ADDR)

def _amount(platform_key):
    p        = CFG['platforms'][platform_key]
    token    = p['token']
    tok      = CFG['tokens'].get(token, {})
    decimals = tok.get('decimals', 18)
    pos_amt  = tok.get('position_amount', 0.01)
    return int(round(pos_amt * 10**decimals))

def supply_platform(platform_key):
    p        = CFG['platforms'][platform_key]
    token    = p['token']
    tok_addr = _token_addr(platform_key)
    amt      = _amount(platform_key)
    ptype    = p['type']

    log.info(f'\n{"─"*60}')
    log.info(f'PLATFORM: {platform_key}  token={token}  type={ptype}  amount={amt}')
    log.info(f'{"─"*60}')

    # ── Step 1: ETH → token ────────────────────────────────────
    if token == 'ETH':
        _rec(platform_key, 'PREPARE', 'OK', note='native ETH, no swap needed')

    elif token == 'WETH':
        try:
            txh = swap.wrap_eth(amt)
            _rec(platform_key, 'WRAP', 'OK', txh)
        except Exception as e:
            _rec(platform_key, 'WRAP', 'FAIL', note=str(e))
            return False

    elif token == 'USDS':
        usdc_amount = amt // 10**12
        # swap ETH → USDC
        try:
            txh = swap.attempt_swap(swap.swap_eth_to_token, USDC_ADDR, usdc_amount)
            _rec(platform_key, 'SWAP->USDC', 'OK', txh or '')
        except (PriceGuardError, ConfigError) as e:
            _rec(platform_key, 'SWAP->USDC', 'FAIL', note=f'price guard: {e}')
            return False
        except SwapExecutionError as e:
            _rec(platform_key, 'SWAP->USDC', 'FAIL', note=str(e))
            return False
        # PSM USDC → USDS
        try:
            executor.psm_swap_usdc_to_usds(usdc_amount)
            _rec(platform_key, 'PSM→USDS', 'OK')
        except Exception as e:
            _rec(platform_key, 'PSM→USDS', 'FAIL', note=str(e))
            return False

    else:
        # Generic ERC20: swap ETH → token
        try:
            txh = swap.attempt_swap(swap.swap_eth_to_token, tok_addr, amt)
            _rec(platform_key, f'SWAP→{token}', 'OK', txh or '')
        except (PriceGuardError, ConfigError) as e:
            _rec(platform_key, f'SWAP→{token}', 'FAIL', note=f'price guard: {e}')
            return False
        except SwapExecutionError as e:
            _rec(platform_key, f'SWAP→{token}', 'FAIL', note=str(e))
            return False

    # ── Step 2: supply/deposit ──────────────────────────────────
    try:
        if ptype == 'comet':
            txh = executor.compound_supply(p['address'], tok_addr, amt)
            _rec(platform_key, 'SUPPLY', 'OK', txh)

        elif ptype == 'erc4626':
            txh = executor.erc4626_deposit(p['address'], tok_addr, amt)
            _rec(platform_key, 'DEPOSIT', 'OK', txh)

        elif ptype == 'ctoken':
            txh = executor.ctoken_supply(p['address'], tok_addr, amt)
            _rec(platform_key, 'MINT', 'OK', txh)

        else:
            _rec(platform_key, 'SUPPLY', 'FAIL', note=f'unknown type: {ptype}')
            return False

    except Exception as e:
        step = {'comet': 'SUPPLY', 'erc4626': 'DEPOSIT', 'ctoken': 'MINT'}.get(ptype, 'SUPPLY')
        _rec(platform_key, step, 'FAIL', note=str(e))
        return False

    # ── Step 3: record position (long expiry — won't auto-expire) ──
    expiry_days = 30
    state.add_position(platform_key, token, amt, expiry_days, txh)
    _rec(platform_key, 'STATE', 'OK', note=f'expiry +{expiry_days}d, held for manual withdraw')
    return True


def print_summary():
    log.info(f'\n{"═"*60}')
    log.info('SUMMARY')
    log.info(f'{"═"*60}')
    by_platform: dict[str, list] = {}
    for r in RESULTS:
        by_platform.setdefault(r['platform'], []).append(r)

    passed = 0
    failed = 0
    for plat, steps in by_platform.items():
        ok    = all(s['status'] == 'OK' for s in steps)
        icon  = '✅' if ok else '❌'
        steps_str = '  '.join(f'{s["step"]}:{s["status"]}' for s in steps)
        log.info(f'{icon} {plat:20s}  {steps_str}')
        if ok:
            passed += 1
        else:
            failed += 1

    log.info(f'\n{"─"*60}')
    log.info(f'PASSED: {passed}/{passed+failed}   FAILED: {failed}/{passed+failed}')
    log.info(f'Log saved: {LOG_PATH}')


if __name__ == '__main__':
    state.init_db()

    w3 = Web3(Web3.HTTPProvider(os.getenv('BASE_RPC_URL', 'https://mainnet.base.org')))
    wallet = Web3.to_checksum_address(os.getenv('WALLET_ADDRESS'))
    bal    = float(Web3.from_wei(w3.eth.get_balance(wallet), 'ether'))

    log.info(f'{"═"*60}')
    log.info(f'BASE AGENT — FULL PLATFORM TEST')
    log.info(f'Wallet : {wallet}')
    log.info(f'Balance: {bal:.5f} ETH')
    log.info(f'Platforms: {len(ALL_PLATFORMS)}  ({", ".join(ALL_PLATFORMS)})')
    log.info(f'{"═"*60}')

    if bal < 0.025:
        log.error(f'ETH too low ({bal:.5f}). Need ≥0.025 ETH for full test.')
        sys.exit(1)

    for platform in ALL_PLATFORMS:
        supply_platform(platform)

    print_summary()
