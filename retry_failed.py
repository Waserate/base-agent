"""
retry_failed.py — Retry 3 failed platforms using tokens already in wallet.

Current wallet state after test_full.py run:
  - USDC: 15  (10 from compound retries + 5 from moonwell swap — supply never happened)
  - USDS: 5   (PSM swap OK but spark deposit failed — vault doesn't accept deposits)
  - wstETH: 0.002 (extra from failed attempt 1 retry — original deposit DID go through)

Actions:
  1. compound_usdc:  supply 5 USDC (USDC already in wallet)
  2. moonwell_usdc:  mint   5 USDC (USDC already in wallet, allowance already MAX)
  3. spark_usds:     SKIP   — vault doesn't accept user deposits on Base
  4. leftover USDC: swap 5 USDC -> ETH (from compound's extra retry buy)
  5. leftover wstETH: swap 0.002 wstETH -> ETH (duplicate from swap retry)
  6. leftover USDS: convert USDS -> USDC -> ETH via PSM + swap
"""
import os, sys, logging
from datetime import datetime
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

os.makedirs('logs', exist_ok=True)
LOG_PATH = f'logs/retry_failed_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'

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

USDC_ADDR = Web3.to_checksum_address('0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')
USDS_ADDR = Web3.to_checksum_address('0x820C137fa70C8691f0e44Dc420a5e53c168921Dc')
WSTETH_ADDR = Web3.to_checksum_address('0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452')

w3 = executor.w3
wallet = executor.WALLET

def bal_token(addr, decimals):
    c = w3.eth.contract(address=addr, abi=executor.ERC20_ABI)
    return c.functions.balanceOf(wallet).call(), c.functions.balanceOf(wallet).call() / 10**decimals

def log_balances(tag=''):
    try:
        eth = float(Web3.from_wei(w3.eth.get_balance(wallet), 'ether'))
        usdc_raw, usdc = bal_token(USDC_ADDR, 6)
        usds_raw, usds = bal_token(USDS_ADDR, 18)
        wsteth_raw, wsteth = bal_token(WSTETH_ADDR, 18)
        log.info(f'[{tag}] ETH={eth:.5f}  USDC={usdc:.2f}  USDS={usds:.2f}  wstETH={wsteth:.6f}')
    except Exception as e:
        log.warning(f'[{tag}] balance check skipped (RPC error): {e}')


if __name__ == '__main__':
    state.init_db()
    log.info('=== retry_failed.py start ===')
    log_balances('start')

    # ── 1. compound_usdc: supply 5 USDC ──────────────────────────────────────────
    log.info('--- [1/5] compound_usdc: supply 5 USDC ---')
    COMPOUND_COMET = Web3.to_checksum_address('0xb125E6687d4313864e53df431d5425969c15Eb2F')
    already = [p for p in state.get_active('compound_usdc')]
    if already:
        log.info(f'compound_usdc already in state.db (id={already[0][0]}) — skipping')
    else:
        try:
            txh = executor.compound_supply(COMPOUND_COMET, USDC_ADDR, 5_000_000)
            state.add_position('compound_usdc', 'USDC', 5_000_000, 30, txh)
            log.info(f'compound_usdc SUPPLY OK: {txh}')
        except Exception as e:
            log.error(f'compound_usdc SUPPLY FAIL: {e}')
    log_balances('after compound')

    # ── 2. moonwell_usdc: mint 5 USDC ────────────────────────────────────────────
    log.info('--- [2/5] moonwell_usdc: mint 5 USDC ---')
    MOONWELL_USDC = Web3.to_checksum_address('0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22')
    already = [p for p in state.get_active('moonwell_usdc')]
    if already:
        log.info(f'moonwell_usdc already in state.db (id={already[0][0]}) — skipping')
    else:
        try:
            txh = executor.ctoken_supply(MOONWELL_USDC, USDC_ADDR, 5_000_000)
            state.add_position('moonwell_usdc', 'USDC', 5_000_000, 30, txh)
            log.info(f'moonwell_usdc MINT OK: {txh}')
        except Exception as e:
            log.error(f'moonwell_usdc MINT FAIL: {e}')
    log_balances('after moonwell')

    # ── 3. spark_usds: SKIP ──────────────────────────────────────────────────────
    log.info('--- [3/5] spark_usds: SKIP (vault does not accept user deposits on Base) ---')

    # ── 4. Swap leftover 5 USDC -> ETH ───────────────────────────────────────────
    log.info('--- [4/5] swap leftover USDC -> ETH ---')
    _, usdc_bal = bal_token(USDC_ADDR, 6)
    if usdc_bal >= 1:
        usdc_raw = int(usdc_bal * 1e6)
        log.info(f'Swapping {usdc_bal:.2f} USDC -> ETH')
        try:
            txh = swap.attempt_swap(swap.swap_token_to_eth, USDC_ADDR, usdc_raw)
            log.info(f'USDC->ETH swap OK: {txh}')
        except Exception as e:
            log.error(f'USDC->ETH swap FAIL: {e}')
    else:
        log.info('No leftover USDC to swap')
    log_balances('after USDC swap')

    # ── 5. Swap leftover wstETH -> ETH ───────────────────────────────────────────
    log.info('--- [5/5] swap leftover wstETH -> ETH ---')
    wsteth_raw, wsteth_bal = bal_token(WSTETH_ADDR, 18)
    if wsteth_raw > 0:
        log.info(f'Swapping {wsteth_bal:.6f} wstETH -> ETH')
        try:
            txh = swap.attempt_swap(swap.swap_token_to_eth, WSTETH_ADDR, wsteth_raw)
            log.info(f'wstETH->ETH swap OK: {txh}')
        except Exception as e:
            log.error(f'wstETH->ETH swap FAIL: {e}')
    else:
        log.info('No leftover wstETH to swap')
    log_balances('after wstETH swap')

    # ── 6. Convert USDS -> USDC -> ETH via PSM ───────────────────────────────────
    log.info('--- BONUS: convert USDS -> USDC -> ETH via PSM ---')
    usds_raw, usds_bal = bal_token(USDS_ADDR, 18)
    if usds_raw > 0:
        log.info(f'PSM: {usds_bal:.2f} USDS -> USDC')
        try:
            usdc_out = executor.psm_swap_usds_to_usdc(usds_raw)
            log.info(f'PSM USDS->USDC OK: received {usdc_out/1e6:.4f} USDC')
            if usdc_out > 0:
                txh = swap.attempt_swap(swap.swap_token_to_eth, USDC_ADDR, usdc_out)
                log.info(f'USDC->ETH swap OK: {txh}')
        except Exception as e:
            log.error(f'USDS->ETH conversion FAIL: {e}')
    log_balances('final')

    log.info('=== retry_failed.py done ===')
    log.info(f'Log: {LOG_PATH}')
