"""
roundtrip_aave_supply.py — Live test one aave_supply platform: supply → wait → withdraw.

Usage:
    python roundtrip_aave_supply.py <platform_key>
    python roundtrip_aave_supply.py aave_usdc
    DRY_RUN=true python roundtrip_aave_supply.py aave_weth

Steps:
    1. Acquire token (wrap ETH or swap ETH → token)
    2. Supply to AAVE v3
    3. Check aToken balance
    4. Wait 15 seconds (simulate hold period)
    5. Withdraw all from AAVE v3
    6. Swap token back to ETH (or unwrap WETH)
    7. Print ETH delta summary

Log: console + logs/roundtrip_aave_supply_<platform>_<ts>.log
"""

import os, sys, json, logging, time
from datetime import datetime
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

AVAILABLE_KEYS = 'aave_usdc aave_weth aave_cbbtc aave_wsteth aave_eurc'

if len(sys.argv) < 2:
    print(f'Usage: python {sys.argv[0]} <platform_key>')
    print(f'Available: {AVAILABLE_KEYS}')
    sys.exit(1)

PLATFORM_KEY = sys.argv[1]

os.makedirs('logs', exist_ok=True)
_ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
_log_file = f'logs/roundtrip_aave_supply_{PLATFORM_KEY}_{_ts}.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(_log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

import executor, swap
import aave_supply

with open('config/contracts.json') as f:
    CFG = json.load(f)

if PLATFORM_KEY not in CFG['platforms']:
    log.error(f'Unknown platform: {PLATFORM_KEY}')
    log.error(f'Available: {AVAILABLE_KEYS}')
    sys.exit(1)

p = CFG['platforms'][PLATFORM_KEY]

if p.get('type') != 'aave_supply':
    log.error(f'{PLATFORM_KEY} is not an aave_supply platform (type={p.get("type")})')
    sys.exit(1)

# ── Derive config values ───────────────────────────────────────────────────────

token_sym   = p['token']
tok_cfg     = CFG['tokens'][token_sym]
tok_addr    = Web3.to_checksum_address(p['token_address'])
atoken_addr = Web3.to_checksum_address(p['atoken_address'])
amount_wei  = int(tok_cfg['position_amount'] * 10**tok_cfg['decimals'])

# ── Main ───────────────────────────────────────────────────────────────────────


def run():
    executor.reset_nonce()

    log.info(f'=== roundtrip_aave_supply: {PLATFORM_KEY} ===')
    eth_before = executor.get_eth_balance()
    log.info(f'ETH before: {eth_before:.6f} ETH')
    log.info(f'Token     : {token_sym}  amount_wei={amount_wei}')
    log.info(f'Log file  : {_log_file}')

    # 1. Acquire token
    log.info('--- ACQUIRE TOKEN ---')
    if token_sym == 'WETH':
        swap.wrap_eth(amount_wei)
    else:
        swap.attempt_swap(swap.swap_eth_to_token, tok_addr, amount_wei)
    time.sleep(3)

    # 2. Supply
    log.info('--- SUPPLY ---')
    txh = aave_supply.supply(tok_addr, amount_wei)
    log.info(f'Supply TX: {txh}')
    time.sleep(4)

    # 3. Check aToken balance
    bal = aave_supply.get_atoken_balance(atoken_addr)
    log.info(f'aToken balance: {bal}  ({bal / 10**tok_cfg["decimals"]:.6f} {token_sym})')

    # 4. Hold
    log.info('Holding 15s...')
    time.sleep(15)

    # 5. Withdraw all
    log.info('--- WITHDRAW ---')
    txh2 = aave_supply.withdraw_all(tok_addr)
    log.info(f'Withdraw TX: {txh2}')
    time.sleep(4)

    # 6. Swap back to ETH
    log.info('--- SWAP BACK ---')
    tok_contract = executor.w3.eth.contract(
        address=tok_addr, abi=executor.ERC20_ABI
    )
    tok_bal = tok_contract.functions.balanceOf(executor.WALLET).call()
    log.info(f'Token balance after withdraw: {tok_bal}')

    if token_sym == 'WETH':
        swap.unwrap_all_weth()
    elif tok_bal > 0:
        swap.attempt_swap(swap.swap_token_to_eth, tok_addr, tok_bal)
    time.sleep(3)

    # 7. Summary
    eth_after = executor.get_eth_balance()
    delta     = eth_after - eth_before
    log.info('')
    log.info(f'ETH before: {eth_before:.6f} ETH')
    log.info(f'ETH after : {eth_after:.6f} ETH')
    log.info(f'ETH delta : {delta:+.6f} ETH')
    log.info(f'=== DONE: {PLATFORM_KEY} ===')
    log.info(f'Log file  : {_log_file}')


if __name__ == '__main__':
    run()
