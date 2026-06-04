"""
test_aero_vote.py — Test Aerodrome veAERO lock + vote + withdraw (Phase 6)

Usage:
    DRY_RUN=true python test_aero_vote.py       # dry-run (no TX)
    python test_aero_vote.py                     # live test (SPENDS ETH)

Live test locks AERO for 7 days (minimum epoch). After lock_end, run:
    python withdraw_all.py
to exit the position.
"""

import os, sys, logging, json
from datetime import datetime, timedelta, timezone
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

os.makedirs('logs', exist_ok=True)
_ts = datetime.now().strftime('%Y%m%d_%H%M%S')
_log_file = f'logs/test_aero_vote_{_ts}.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(_log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

import state
import executor
from aero_vote import aero_vote_enter, VE_ADDR, VE_ABI

DRY_RUN = executor.DRY_RUN
WALLET  = executor.WALLET
w3      = executor.w3

LOCK_DAYS = 7  # minimum epoch for test

def run():
    state.init_db()

    eth_start = w3.eth.get_balance(WALLET)
    log.info('=' * 72)
    log.info(f'TEST AERO VOTE  {"[DRY RUN]" if DRY_RUN else "[LIVE]"}')
    log.info(f'Wallet     : {WALLET}')
    log.info(f'ETH start  : {Web3.from_wei(eth_start, "ether"):.6f}')
    log.info(f'Lock days  : {LOCK_DAYS} (will round up to next WEEK boundary)')
    log.info(f'Log file   : {_log_file}')
    log.info('=' * 72)

    # Run aero_vote_enter
    log.info('')
    log.info('--- aero_vote_enter ---')
    result = aero_vote_enter(lock_days=LOCK_DAYS)

    log.info('')
    log.info('--- Result ---')
    log.info(f'  tokenId    : {result["token_id"]}')
    log.info(f'  AERO locked: {result["aero_wei"] / 1e18:.4f} AERO')
    log.info(f'  tx_lock    : {result["tx_lock"]}')
    log.info(f'  tx_vote    : {result["tx_vote"]}')

    if DRY_RUN:
        lock_end_dt = datetime.fromtimestamp(result["lock_end"], tz=timezone.utc)
        log.info(f'  lock_end   : {lock_end_dt.strftime("%Y-%m-%d %H:%M UTC")} [simulated]')
    else:
        # Verify on-chain
        ve = w3.eth.contract(address=Web3.to_checksum_address(VE_ADDR), abi=VE_ABI)
        locked = ve.functions.locked(result["token_id"]).call()
        lock_end_ts = locked[1]
        lock_end_dt = datetime.fromtimestamp(lock_end_ts, tz=timezone.utc)
        log.info(f'  lock_end   : {lock_end_dt.strftime("%Y-%m-%d %H:%M UTC")} (on-chain confirmed)')

    # Save to state.db for withdraw_all.py
    from datetime import date
    amount_wei_str = f'{result["token_id"]}|{result["aero_wei"]}'
    lock_end_date  = datetime.fromtimestamp(result["lock_end"]).date()
    expiry_days    = (lock_end_date - date.today()).days

    if not DRY_RUN:
        state.add_position(
            platform    = 'aero_vote',
            token       = 'AERO',
            amount_wei  = amount_wei_str,
            expiry_days = expiry_days,
            tx_hash     = result["tx_lock"],
        )
        log.info(f'  Saved to state.db: platform=aero_vote  expiry={lock_end_date}')
    else:
        log.info(f'  [DRY RUN] state.db not updated')

    # ETH delta
    eth_end = w3.eth.get_balance(WALLET)
    eth_spent = (eth_start - eth_end) / 1e18
    log.info('')
    log.info(f'ETH start  : {eth_start / 1e18:.6f}')
    log.info(f'ETH end    : {eth_end / 1e18:.6f}')
    log.info(f'ETH spent  : {eth_spent:.6f}')
    log.info('=' * 72)
    log.info('TEST COMPLETE')
    log.info(f'To exit after lock_end ({lock_end_date}): python withdraw_all.py')
    log.info('=' * 72)


if __name__ == '__main__':
    run()
