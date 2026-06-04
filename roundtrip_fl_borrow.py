"""
roundtrip_fl_borrow.py — Live test Fluid T1 borrow open+close cycle.

Usage:
  python roundtrip_fl_borrow.py <platform_key>             # full open+close
  python roundtrip_fl_borrow.py <platform_key> --open-only # open only, print encoded state
  python roundtrip_fl_borrow.py <platform_key> --close-encoded "nftId:1234||COL:wstETH:2000000000000000||BOR:cbBTC:6800"

  DRY_RUN=true python roundtrip_fl_borrow.py fl_eth_cbbtc

Available platforms:
  fl_eth_cbbtc      ETH  -> cbBTC  CF=86% util=9%  (Tier 1 — LOW util)
  fl_wsteth_cbbtc   wstETH -> cbBTC CF=85% util=9%
  fl_wsteth_eth     wstETH -> ETH   CF=93% correlated
  fl_cbbtc_eth      cbBTC  -> ETH   CF=86% correlated
  fl_eth_usdc       ETH  -> USDC   CF=85% util=97%
  fl_wsteth_usdc    wstETH -> USDC  CF=80%
  fl_cbbtc_usdc     cbBTC  -> USDC  CF=80%
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

import fluid_borrow as fl

with open('config/contracts.json') as f:
    CFG = json.load(f)


def get_platform(key: str) -> dict:
    p = CFG['platforms'].get(key)
    if not p:
        raise SystemExit(f'Platform not found: {key}')
    if p.get('type') != 'fluid_borrow':
        raise SystemExit(f'{key} is type={p.get("type")}, expected fluid_borrow')
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
        idx       = args.index('--close-encoded')
        close_enc = args[idx + 1]

    p = get_platform(platform_key)
    log.info(f'=== Fluid Borrow Roundtrip: {platform_key} ===')
    log.info(f'  {p["display_name"]}')
    log.info(f'  Vault:      {p["vault_address"]}')
    log.info(f'  Collateral: {p["collateral_token"]} {p["collateral_amount_wei"]}wei '
             f'CF={p["collateral_cf"]:.0%}')
    log.info(f'  Borrow:     {p["borrow_token"]}  LTV {p["ltv_min"]:.0%}-{p["ltv_max"]:.0%}')
    log.info(f'  DRY_RUN={DRY_RUN}  Wallet={executor.WALLET}')

    bal_before = eth_balance()
    log.info(f'ETH before: {bal_before:.6f}')

    if close_enc:
        log.info(f'--- CLOSE only (encoded={close_enc}) ---')
        health = fl.check_health(close_enc, p)
        log.info(f'Health (estimated): {health:.2f}x')
        close_txh = fl.close_borrow(close_enc, p)
        log.info(f'Closed: {close_txh}')

    else:
        # OPEN
        log.info('--- OPEN ---')
        encoded, open_txh = fl.open_borrow(p)
        log.info(f'Open tx: {open_txh}')
        log.info(f'State:   {encoded}')

        parsed = fl.parse_state(encoded)
        log.info(f'NFT ID:  {parsed["nft_id"]}')
        if parsed['nft_id'] == 0 and not DRY_RUN:
            log.warning('NFT ID=0 — Transfer event not found in receipt. Check TX manually.')

        health = fl.check_health(encoded, p)
        log.info(f'Health (estimated): {health:.2f}x')

        if open_only:
            log.info('--open-only: stopping here.')
            log.info(f'To close later:')
            log.info(f'  python roundtrip_fl_borrow.py {platform_key} '
                     f'--close-encoded "{encoded}"')
        else:
            log.info('Waiting 5s before close...')
            if not DRY_RUN:
                time.sleep(5)

            log.info('--- CLOSE ---')
            close_txh = fl.close_borrow(encoded, p)
            log.info(f'Close tx: {close_txh}')

    bal_after = eth_balance()
    cost = bal_before - bal_after
    eth_price = executor.get_token_usd_price('WETH')
    log.info(f'ETH after:  {bal_after:.6f}')
    log.info(f'Gas cost:   {cost:.6f} ETH (~${cost * eth_price:.2f})')
    log.info('=== DONE ===')


if __name__ == '__main__':
    main()
