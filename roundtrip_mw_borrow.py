"""
roundtrip_mw_borrow.py — Live test Moonwell borrow open+close cycle.

Usage:
  python roundtrip_mw_borrow.py <platform_key>             # full open+close
  python roundtrip_mw_borrow.py <platform_key> --open-only # open only, print encoded state
  python roundtrip_mw_borrow.py <platform_key> --close-encoded "WETH:2500000000000000||USDC:875000"

DRY_RUN=true python roundtrip_mw_borrow.py mw_weth_usdc
"""

import sys, os, json, logging, time
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

DRY_RUN = os.getenv('DRY_RUN', '').lower() in ('1', 'true', 'yes')

import executor
executor.reset_nonce()

import moonwell_borrow as mw

with open('config/contracts.json') as f:
    CFG = json.load(f)


def get_platform(key: str) -> dict:
    p = CFG['platforms'].get(key)
    if not p:
        raise SystemExit(f'Platform not found: {key}')
    if p.get('type') != 'mw_borrow':
        raise SystemExit(f'{key} is type={p.get("type")}, not mw_borrow')
    return p


def eth_balance() -> float:
    return float(Web3.from_wei(executor.w3.eth.get_balance(executor.WALLET), 'ether'))


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    platform_key = args[0]
    open_only    = '--open-only' in args
    close_enc    = None
    if '--close-encoded' in args:
        idx = args.index('--close-encoded')
        close_enc = args[idx + 1]

    p = get_platform(platform_key)
    log.info(f'Platform: {platform_key}  ({p["name"]})')
    log.info(f'Collateral: {p["collateral_token"]} {p["collateral_amount_wei"]}  '
             f'CF={p["collateral_cf"]:.0%}')
    log.info(f'Borrow: {p["borrow_token"]}  LTV {p["ltv_min"]:.0%}-{p["ltv_max"]:.0%}')
    log.info(f'DRY_RUN={DRY_RUN}')
    log.info(f'Wallet: {executor.WALLET}')

    bal_before = eth_balance()
    log.info(f'ETH balance before: {bal_before:.6f}')

    if close_enc:
        # close only
        log.info(f'--- CLOSE (encoded={close_enc}) ---')
        health = mw.check_health(close_enc, p)
        log.info(f'Health before close: {health:.2f}x')
        mw.close_borrow(close_enc, p, pos_id=-1, dry=DRY_RUN)
    else:
        # open
        log.info('--- OPEN ---')
        avail = mw.check_availability(p)
        log.info(f'Availability: {avail}')

        encoded = mw.open_borrow(p)
        log.info(f'Encoded state: {encoded}')

        time.sleep(3)
        health = mw.check_health(encoded, p)
        log.info(f'Health after open: {health:.2f}x')

        if open_only:
            log.info(f'--open-only: stopping. To close:')
            log.info(f'  python roundtrip_mw_borrow.py {platform_key} --close-encoded "{encoded}"')
        else:
            log.info('--- CLOSE ---')
            mw.close_borrow(encoded, p, pos_id=-1, dry=DRY_RUN)

    bal_after = eth_balance()
    cost = bal_before - bal_after
    log.info(f'ETH balance after:  {bal_after:.6f}')
    log.info(f'Gas cost: {cost:.6f} ETH  (~${cost * executor.get_token_usd_price("WETH"):.2f})')


if __name__ == '__main__':
    main()
