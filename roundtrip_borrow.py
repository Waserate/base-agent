"""
roundtrip_borrow.py — Live test one compound_borrow platform: open → wait → close.

Usage:
    python roundtrip_borrow.py <platform_key>
    python roundtrip_borrow.py cb_usdc_weth
    DRY_RUN=true python roundtrip_borrow.py cb_usdc_multi

Steps:
    1. Check Comet utilization (skip if unavailable)
    2. open_borrow  → supply collateral(s) → borrow base token → swap to ETH
    3. Print position summary + health factor
    4. Wait 10 seconds (simulate hold period — increase manually for real hold)
    5. close_borrow → repay → withdraw collateral(s) → convert to ETH
    6. Print ETH delta summary

Log: console + logs/roundtrip_borrow_<platform>_<ts>.log
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
    print(' '.join(cfg.get('phase_borrow', [])))
    sys.exit(1)

PLATFORM_KEY = sys.argv[1]
OPEN_ONLY    = '--open-only' in sys.argv

os.makedirs('logs', exist_ok=True)
_ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
_log_file = f'logs/roundtrip_borrow_{PLATFORM_KEY}_{_ts}.log'

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
import state
import compound_borrow as cb

with open('config/contracts.json') as f:
    CFG = json.load(f)

if PLATFORM_KEY not in CFG['platforms']:
    log.error(f'Unknown platform: {PLATFORM_KEY}')
    log.error(f'Available: {CFG.get("phase_borrow", [])}')
    sys.exit(1)

p    = CFG['platforms'][PLATFORM_KEY]
DRY  = executor.DRY_RUN

if p.get('type') != 'compound_borrow':
    log.error(f'{PLATFORM_KEY} is not a compound_borrow platform (type={p.get("type")})')
    sys.exit(1)

# ── Main ───────────────────────────────────────────────────────────────────────

CLOSE_ENCODED = None
for i, arg in enumerate(sys.argv):
    if arg == '--close-encoded' and i + 1 < len(sys.argv):
        CLOSE_ENCODED = sys.argv[i + 1]


def run():
    state.init_db()
    executor.reset_nonce()

    # Close-only mode: just close a previously opened position
    if CLOSE_ENCODED:
        log.info(f'=== CLOSE-ONLY MODE ===')
        log.info(f'Platform : {PLATFORM_KEY}')
        log.info(f'Encoded  : {CLOSE_ENCODED}')
        eth_before = executor.w3.eth.get_balance(executor.WALLET)
        try:
            txh = cb.close_borrow(CLOSE_ENCODED, p)
            eth_after = executor.w3.eth.get_balance(executor.WALLET)
            delta = eth_after - eth_before
            log.info(f'Close TX : {txh}')
            log.info(f'ETH delta: {delta/1e18:+.6f}')
            log.info('PASS')
        except Exception as e:
            log.error(f'close_borrow FAILED: {e}')
        return

    eth_start = executor.w3.eth.get_balance(executor.WALLET)
    eth_usd   = executor.get_eth_usd_price()

    log.info('=' * 64)
    log.info(f'ROUNDTRIP BORROW  {"[DRY RUN]" if DRY else "[LIVE]"}')
    log.info(f'Platform : {PLATFORM_KEY}')
    log.info(f'Comet    : {p["comet_address"]}')
    log.info(f'Borrow   : {p["borrow_token"]}')
    log.info(f'ETH start: {Web3.from_wei(eth_start, "ether"):.6f} ETH  (${eth_start/1e18*eth_usd:.2f})')
    log.info(f'Log file : {_log_file}')
    log.info('=' * 64)

    # 1. Check availability
    status = cb.check_availability(
        p['comet_address'],
        float(p.get('max_utilization', 0.90))
    )
    log.info(f'Comet util: {status["utilization"]:.2%}  available={status["available"]}')
    if not status['available']:
        log.error(f'SKIP: Comet not available (util={status["utilization"]:.1%} >= max={p.get("max_utilization",0.90):.0%})')
        sys.exit(0)

    # 2. Open borrow
    log.info('')
    log.info('--- OPEN BORROW ---')
    eth_before_open = executor.w3.eth.get_balance(executor.WALLET)
    try:
        encoded, txh_open = cb.open_borrow(p)
    except Exception as e:
        log.error(f'open_borrow FAILED: {e}')
        sys.exit(1)

    if not DRY:
        time.sleep(4)

    eth_after_open = executor.w3.eth.get_balance(executor.WALLET)
    open_delta     = eth_after_open - eth_before_open

    log.info(f'Encoded state : {encoded}')
    log.info(f'Open TX       : {txh_open}')
    log.info(f'ETH delta open: {open_delta/1e18:+.6f} ETH  (gas + borrow swap)')

    # 3. Health check
    log.info('')
    health = cb.check_health(encoded, p)
    log.info(f'Health factor : {health:.2f}x  (liquidation at 1.0x)')
    if health < 999:
        log.info(f'              : {"SAFE" if health >= 1.5 else "WARNING: near threshold!"}')

    # parse collaterals for display
    collaterals, borrow_info = cb.parse_state(encoded)
    log.info(f'Collaterals   : {[(c["token"], c["wei"]) for c in collaterals]}')
    log.info(f'Borrowed      : {borrow_info["wei"] / 10**p.get("borrow_decimals",6):.6f} {borrow_info["token"]}')

    # 4. Stop here if --open-only
    if OPEN_ONLY:
        log.info('')
        log.info('=== OPEN-ONLY MODE — position left open ===')
        log.info(f'To close later run:')
        log.info(f'  python roundtrip_borrow.py {PLATFORM_KEY} --close-encoded "{encoded}"')
        log.info('=' * 64)
        return

    hold_secs = 10 if not DRY else 0
    if hold_secs > 0:
        log.info(f'')
        log.info(f'Holding {hold_secs}s...')
        time.sleep(hold_secs)

    # 5. Close borrow
    log.info('')
    log.info('--- CLOSE BORROW ---')
    executor.reset_nonce()
    eth_before_close = executor.w3.eth.get_balance(executor.WALLET)
    try:
        txh_close = cb.close_borrow(encoded, p)
    except Exception as e:
        log.error(f'close_borrow FAILED: {e}')
        log.error(f'Position still open. Encoded: {encoded}')
        log.error(f'Run manually: python -c "import compound_borrow as cb, json; ...')
        sys.exit(1)

    if not DRY:
        time.sleep(4)

    eth_after_close = executor.w3.eth.get_balance(executor.WALLET)
    close_delta     = eth_after_close - eth_before_close
    net_delta       = eth_after_close - eth_start
    eth_usd_end     = executor.get_eth_usd_price()

    # 6. Summary
    log.info('')
    log.info('=' * 64)
    log.info('ROUNDTRIP SUMMARY')
    log.info(f'  Platform  : {PLATFORM_KEY}')
    log.info(f'  Open TX   : {txh_open}')
    log.info(f'  Close TX  : {txh_close}')
    log.info(f'  ETH start : {eth_start/1e18:.6f}')
    log.info(f'  ETH end   : {eth_after_close/1e18:.6f}')
    log.info(f'  Open delta: {open_delta/1e18:+.6f} ETH')
    log.info(f'  Close delta:{close_delta/1e18:+.6f} ETH')
    log.info(f'  NET delta : {net_delta/1e18:+.6f} ETH  (${net_delta/1e18*eth_usd_end:+.4f} gas cost)')
    log.info(f'  Log file  : {_log_file}')
    log.info('=' * 64)

    result = 'PASS' if not DRY else 'PASS (DRY)'
    log.info(f'  Result    : {result}')


if __name__ == '__main__':
    run()
