"""
roundtrip_aave_borrow.py — Live test one aave_borrow platform: open → wait → close.

Usage:
    python roundtrip_aave_borrow.py <platform_key>
    python roundtrip_aave_borrow.py aav_weth_usdc
    DRY_RUN=true python roundtrip_aave_borrow.py aav_cbbtc_usdc

Steps:
    1. open_borrow  → supply collateral → borrow token
    2. check_health → assert above threshold
    3. Wait 15 seconds (simulate hold period)
    4. close_borrow → repay → withdraw collateral
    5. Print ETH delta summary

Log: console + logs/roundtrip_aave_borrow_<platform>_<ts>.log
"""

import os, sys, json, logging, time
from datetime import datetime
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

if len(sys.argv) < 2:
    print(f'Usage: python {sys.argv[0]} <platform_key>')
    print('Available:', end=' ')
    with open('config/contracts.json') as f:
        cfg = json.load(f)
    print(' '.join(cfg.get('phase_aave_borrow', [])))
    sys.exit(1)

PLATFORM_KEY = sys.argv[1]

os.makedirs('logs', exist_ok=True)
_ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
_log_file = f'logs/roundtrip_aave_borrow_{PLATFORM_KEY}_{_ts}.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(_log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

import executor
import aave_borrow

with open('config/contracts.json') as f:
    CFG = json.load(f)

if PLATFORM_KEY not in CFG['platforms']:
    log.error(f'Unknown platform: {PLATFORM_KEY}')
    log.error(f'Available: {CFG.get("phase_aave_borrow", [])}')
    sys.exit(1)

p   = CFG['platforms'][PLATFORM_KEY]
DRY = executor.DRY_RUN

if p.get('type') != 'aave_borrow':
    log.error(f'{PLATFORM_KEY} is not an aave_borrow platform (type={p.get("type")})')
    sys.exit(1)

# ── Main ───────────────────────────────────────────────────────────────────────

def run():
    executor.reset_nonce()

    eth_before = executor.get_eth_balance()

    log.info('=' * 64)
    log.info(f'=== roundtrip_aave_borrow: {PLATFORM_KEY} ===')
    log.info(f'ETH before: {eth_before:.6f}')
    log.info(f'Log file  : {_log_file}')
    log.info('=' * 64)

    # 1. Open borrow
    log.info('')
    log.info('--- OPEN BORROW ---')
    try:
        encoded, txh = aave_borrow.open_borrow(p)
    except Exception as e:
        log.error(f'open_borrow FAILED: {e}')
        sys.exit(1)

    log.info(f'Encoded state: {encoded}')
    log.info(f'Open TX      : {txh}')

    # 2. Health check
    log.info('')
    hf = aave_borrow.check_health(encoded, p)
    log.info(f'Health factor: {hf:.2f}x')
    assert hf > aave_borrow.HEALTH_CLOSE_THRESHOLD, \
        f'Health {hf:.2f}x below threshold after open!'

    # 3. Mid balance + hold
    eth_mid = executor.get_eth_balance()
    log.info(f'ETH mid     : {eth_mid:.6f}')
    log.info('')
    log.info('Holding 15s...')
    time.sleep(15)

    # 4. Close borrow
    log.info('')
    log.info('--- CLOSE BORROW ---')
    try:
        close_txh = aave_borrow.close_borrow(encoded, p)
    except Exception as e:
        log.error(f'close_borrow FAILED: {e}')
        log.error(f'Position still open. Encoded: {encoded}')
        sys.exit(1)

    log.info(f'Close TX: {close_txh}')

    # 5. Summary
    eth_after = executor.get_eth_balance()
    delta     = eth_after - eth_before

    log.info('')
    log.info('=' * 64)
    log.info(f'ETH after:  {eth_after:.6f}')
    log.info(f'ETH delta:  {delta:+.6f} ETH')
    log.info(f'=== DONE: {PLATFORM_KEY} ===')
    log.info(f'Log file : {_log_file}')
    log.info('=' * 64)


if __name__ == '__main__':
    run()
