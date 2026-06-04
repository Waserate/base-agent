"""
sweep_tokens.py — Convert all non-ETH token balances in wallet to ETH.

Checks USDC, USDS, sUSDS, WETH, wstETH, EURC, cbBTC.
For each non-zero balance: swap/unwrap to ETH.
Use after withdraw_all.py when RPC stale leaves tokens un-swapped.

Usage:
    python sweep_tokens.py
    DRY_RUN=true python sweep_tokens.py
"""

import os, sys, json, logging
from datetime import datetime
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

import executor, swap
from swap import PriceGuardError, ConfigError, SwapExecutionError
import rule_engine as _rule_engine

os.makedirs('logs', exist_ok=True)
_ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
_log_file = f'logs/sweep_{_ts}.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(_log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as f:
    CFG = json.load(f)

w3     = executor.w3
WALLET = executor.WALLET
PSM3   = executor.PSM3_ADDR
USDC_ADDR  = CFG['tokens']['USDC']['address']
USDS_ADDR  = CFG['tokens']['USDS']['address']
SUSDS_ADDR = CFG['tokens']['sUSDS']['address']

def _bal_wei(addr: str) -> int:
    c = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=executor.ERC20_ABI)
    return c.functions.balanceOf(WALLET).call()

def _eth_wei() -> int:
    return w3.eth.get_balance(WALLET)

TOKENS = [
    # (symbol, address, decimals, convert_fn)
    # convert_fn: callable(addr, amount_wei) -> label_or_txh
    ('WETH',   CFG['tokens']['WETH']['address'],   18, 'unwrap'),
    ('USDC',   CFG['tokens']['USDC']['address'],   6,  'swap_to_eth'),
    ('USDS',   CFG['tokens']['USDS']['address'],   18, 'psm_usds_then_eth'),
    ('sUSDS',  CFG['tokens']['sUSDS']['address'],  6,  'psm_susds_then_eth'),
    ('wstETH', CFG['tokens']['wstETH']['address'], 18, 'swap_to_eth'),
    ('EURC',   CFG['tokens']['EURC']['address'],   6,  'swap_to_eth'),
    ('cbBTC',  CFG['tokens']['cbBTC']['address'],  8,  'swap_to_eth'),
    ('AERO',   CFG['tokens']['AERO']['address'],   18, 'swap_to_eth'),
    ('VIRTUAL',CFG['tokens']['VIRTUAL']['address'],18, 'swap_to_eth'),
    ('USDT',   CFG['tokens']['USDT']['address'],    6, 'swap_to_eth'),
    ('CAKE',   CFG['tokens']['CAKE']['address'],   18, 'swap_to_eth'),
]

def _effective_sweep_amount(symbol: str, balance_wei: int) -> int:
    """
    Rules 24-26: return sweep amount for token.
    USDC/WETH: only sweep excess above retention threshold.
    All other tokens: sweep full balance.
    """
    if symbol == 'USDC':
        return _rule_engine.usdc_excess(balance_wei)
    if symbol == 'WETH':
        return _rule_engine.weth_excess(balance_wei)
    return balance_wei

def _sweep_one(symbol: str, addr: str, decimals: int, mode: str, bal_wei: int) -> str:
    """Convert bal_wei of token to ETH. Returns tx label."""
    addr = Web3.to_checksum_address(addr)

    if mode == 'unwrap':
        swap.unwrap_weth(bal_wei)
        return 'unwrap_done'

    if mode == 'psm_usds_then_eth':
        usdc = executor.psm_swap_usds_to_usdc(bal_wei)
        return swap.attempt_swap(swap.swap_token_to_eth, USDC_ADDR, usdc)

    if mode == 'psm_susds_then_eth':
        usdc = executor.psm_swap_susds_to_usdc(bal_wei)
        return swap.attempt_swap(swap.swap_token_to_eth, USDC_ADDR, usdc)

    if mode == 'swap_to_eth':
        return swap.attempt_swap(swap.swap_token_to_eth, addr, bal_wei)

    raise ValueError(f'Unknown mode: {mode}')


def run():
    import step_logger as _sl
    _sl.set_context('sweep_tokens', 'Sweep Tokens')
    _sl.slog('start', f'checking {len(TOKENS)} tokens')
    dry = executor.DRY_RUN
    eth_start = _eth_wei()

    log.info('=' * 65)
    log.info(f'SWEEP TOKENS  {"[DRY RUN]" if dry else "[LIVE]"}')
    log.info(f'Wallet  : {WALLET}')
    log.info(f'ETH     : {eth_start / 1e18:.6f}')
    log.info(f'Log     : {_log_file}')
    log.info('=' * 65)

    results = []

    for symbol, addr, decimals, mode in TOKENS:
        bal       = _bal_wei(addr)
        sweep_amt = _effective_sweep_amount(symbol, bal)
        human     = bal / 10**decimals
        retain    = (bal - sweep_amt) / 10**decimals

        if bal == 0:
            log.info(f'  {symbol:6}  {human:.6f}  skip (zero)')
            continue
        if sweep_amt == 0:
            log.info(f'  {symbol:6}  {human:.6f}  skip (retain threshold — keeping all)')
            continue
        if sweep_amt < bal:
            log.info(f'  {symbol:6}  {human:.6f}  retaining {retain:.6f}  sweeping {sweep_amt/10**decimals:.6f} ...')
        else:
            log.info(f'  {symbol:6}  {human:.6f}  -> converting to ETH ...')

        eth_before = _eth_wei()

        try:
            txh = _sweep_one(symbol, addr, decimals, mode, sweep_amt)
            eth_after = _eth_wei()
            delta = (eth_after - eth_before) / 1e18
            log.info(f'         OK  tx={txh}  ETH delta: {delta:+.6f}')
            results.append((symbol, human, 'OK', delta, txh))
        except (PriceGuardError, ConfigError) as e:
            log.warning(f'         SKIPPED: {e}')
            results.append((symbol, human, 'SKIPPED', 0.0, str(e)[:60]))
        except Exception as e:
            log.error(f'         FAILED: {e}')
            results.append((symbol, human, 'FAILED', 0.0, str(e)[:60]))

    eth_end   = _eth_wei()
    net_delta = (eth_end - eth_start) / 1e18

    log.info('')
    log.info('=' * 65)
    log.info('SWEEP SUMMARY')
    log.info(f'  {"Token":6}  {"Amount":>12}  {"Status":8}  {"ETH Delta":>10}')
    log.info('  ' + '-' * 50)
    for sym, amt, status, delta, _ in results:
        log.info(f'  {sym:6}  {amt:>12.6f}  {status:8}  {delta:>+10.6f}')
    log.info('  ' + '-' * 50)
    log.info(f'  {"NET":6}  {"":>12}  {"":8}  {net_delta:>+10.6f}')
    log.info(f'  ETH before : {eth_start / 1e18:.6f}')
    log.info(f'  ETH after  : {eth_end / 1e18:.6f}')
    log.info(f'  Log file   : {_log_file}')
    log.info('=' * 65)


if __name__ == '__main__':
    run()
