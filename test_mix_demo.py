"""
test_mix_demo.py — 5-round mixed integration test: LP + Borrow + Lend per round.

Each round:
  1. LP:    acquire tokens -> mint position -> hold 15s -> close -> ETH
  2. Borrow: open (collateral -> borrow) -> health check -> hold 15s -> close -> ETH
  3. Lend:  acquire token -> supply -> hold 15s -> withdraw -> ETH

Usage:
    python test_mix_demo.py
    DRY_RUN=true python test_mix_demo.py

Log: console + logs/test_mix_demo_<ts>.log

5 rounds x 3 steps = 15 operations total.
"""

import os, sys, json, logging, time
from datetime import datetime
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()
os.makedirs('logs', exist_ok=True)

_ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
_log_file = f'logs/test_mix_demo_{_ts}.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(_log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

import state, executor, swap, uni_lp, pancake_lp
import aave_supply, aave_borrow, compound_borrow as cb, moonwell_borrow as mw, fluid_borrow as fl
from swap import PriceGuardError, ConfigError, SwapExecutionError

with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as f:
    CFG = json.load(f)

DRY_RUN   = executor.DRY_RUN
HOLD_SECS = 15  # hold between open and close per step

# ── 5 rounds — platform combos ───────────────────────────────────────────────
# Each tuple: (lp_key, lp_type, borrow_key, borrow_type, lend_key)
ROUNDS = [
    ('uni_lp_weth_usdc_3000',     'uni_lp',    'aav_weth_usdc',  'aave_borrow', 'aave_usdc'),
    ('pancake_lp_weth_usdc_100',  'pancake_lp', 'mw_weth_usdc',   'mw_borrow',   'aave_weth'),
    ('uni_lp_usdc_cbbtc_500',     'uni_lp',    'fl_eth_usdc',    'fluid_borrow','aave_eurc'),
    ('pancake_lp_eurc_usdc_100',  'pancake_lp', 'cb_usdc_weth',   'compound_borrow','aave_cbbtc'),
    ('uni_lp_eurc_usdc_500',      'uni_lp',    'aav_cbbtc_usdc', 'aave_borrow', 'aave_wsteth'),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def eth_bal() -> float:
    return executor.get_eth_balance()


def _acquire(sym: str, addr: str, amt: int) -> bool:
    if sym == 'WETH':
        try:
            swap.wrap_eth(amt)
            log.info(f'  WRAP -> WETH OK')
            return True
        except Exception as e:
            log.error(f'  WRAP FAIL: {e}')
            return False
    try:
        swap.attempt_swap(swap.swap_eth_to_token, addr, amt)
        log.info(f'  SWAP -> {sym} OK')
        return True
    except (PriceGuardError, ConfigError, SwapExecutionError) as e:
        log.error(f'  SWAP -> {sym} FAIL: {e}')
        return False


def _to_eth(sym: str, addr: str):
    c = executor.w3.eth.contract(
        address=Web3.to_checksum_address(addr), abi=executor.ERC20_ABI
    )
    bal = c.functions.balanceOf(executor.WALLET).call()
    if bal == 0:
        return
    if sym == 'WETH':
        swap.unwrap_all_weth()
        log.info(f'  UNWRAP WETH OK')
    else:
        swap.attempt_swap(swap.swap_token_to_eth, addr, bal)
        log.info(f'  SWAP {sym} -> ETH OK')


# ── LP step ───────────────────────────────────────────────────────────────────

def step_lp(pool_key: str, lp_type: str) -> tuple:
    """Open + close LP position. Returns (PASS|FAIL, eth_delta)."""
    p       = CFG['platforms'][pool_key]
    t0_sym  = p['token0']
    t1_sym  = p['token1']
    t0_addr = Web3.to_checksum_address(p['token0_address'])
    t1_addr = Web3.to_checksum_address(p['token1_address'])
    t0_dec  = CFG['tokens'].get(t0_sym, {}).get('decimals', 18)
    t1_dec  = CFG['tokens'].get(t1_sym, {}).get('decimals', 18)

    budget = 5.0
    p0 = executor.get_token_usd_price(t0_sym)
    p1 = executor.get_token_usd_price(t1_sym)
    amt0 = int(budget / 2 / p0 * 10**t0_dec)
    amt1 = int(budget / 2 / p1 * 10**t1_dec)

    log.info(f'  [{lp_type}] {pool_key}  ${budget/2:.2f} {t0_sym} + ${budget/2:.2f} {t1_sym}')

    eth_before = eth_bal()

    # Acquire tokens (non-WETH first)
    pairs = [(t0_sym, t0_addr, amt0), (t1_sym, t1_addr, amt1)]
    pairs.sort(key=lambda x: 1 if x[0] == 'WETH' else 0)
    for sym, addr, amt in pairs:
        if not _acquire(sym, addr, amt):
            return 'FAIL', 0.0

    # Mint
    try:
        executor.reset_nonce()
        if lp_type == 'uni_lp':
            token_id, txh = uni_lp.mint_uni_lp(pool_key)
        else:
            token_id, txh = pancake_lp.mint_pancake_lp(pool_key)
        log.info(f'  MINT OK  tokenId={token_id}  tx={txh[:22]}...')
    except Exception as e:
        log.error(f'  MINT FAIL: {e}')
        return 'FAIL', 0.0

    log.info(f'  Holding {HOLD_SECS}s...')
    time.sleep(HOLD_SECS)

    # Close
    try:
        executor.reset_nonce()
        if lp_type == 'uni_lp':
            uni_lp.close_uni_lp(token_id)
        else:
            pancake_lp.close_pancake_lp(token_id)
        log.info(f'  CLOSE OK')
    except Exception as e:
        log.error(f'  CLOSE FAIL: {e}')
        return 'FAIL', 0.0

    time.sleep(4)

    # Convert back
    for sym, addr in [(t0_sym, t0_addr), (t1_sym, t1_addr)]:
        try:
            _to_eth(sym, addr)
        except Exception as e:
            log.warning(f'  to_eth {sym} skip: {e}')

    delta = eth_bal() - eth_before
    log.info(f'  LP delta: {delta:+.6f} ETH')
    return 'PASS', delta


# ── Borrow step ───────────────────────────────────────────────────────────────

def step_borrow(platform_key: str, borrow_type: str) -> tuple:
    """Open + close borrow position. Returns (PASS|FAIL, eth_delta)."""
    p = CFG['platforms'][platform_key]
    log.info(f'  [{borrow_type}] {platform_key}  {p.get("display_name", platform_key)}')

    eth_before = eth_bal()

    try:
        executor.reset_nonce()
        if borrow_type == 'aave_borrow':
            encoded, txh = aave_borrow.open_borrow(p)
            log.info(f'  OPEN OK  tx={txh[:22]}...')
            hf = aave_borrow.check_health(encoded, p)
            log.info(f'  Health: {hf:.2f}x')
            log.info(f'  Holding {HOLD_SECS}s...')
            time.sleep(HOLD_SECS)
            executor.reset_nonce()
            close_txh = aave_borrow.close_borrow(encoded, p)
            log.info(f'  CLOSE OK  tx={close_txh[:22]}...')

        elif borrow_type == 'mw_borrow':
            avail = mw.check_availability(p)
            if not avail['available']:
                log.warning(f'  SKIP — util={avail["utilization"]:.1%} cap_exceeded={avail.get("cap_exceeded",False)}')
                return 'SKIP', 0.0
            encoded = mw.open_borrow(p)
            log.info(f'  OPEN OK  encoded={encoded[:40]}...')
            hf = mw.check_health(encoded, p)
            log.info(f'  Health: {hf:.2f}x')
            log.info(f'  Holding {HOLD_SECS}s...')
            time.sleep(HOLD_SECS)
            executor.reset_nonce()
            # pos_id=0 — test-only, no state.db row written, close_position(0) is a no-op
            mw.close_borrow(encoded, p, 0)
            log.info(f'  CLOSE OK')

        elif borrow_type == 'fluid_borrow':
            encoded, txh = fl.open_borrow(p)
            log.info(f'  OPEN OK  tx={txh[:22]}...')
            hf = fl.check_health(encoded, p)
            log.info(f'  Health: {hf:.2f}x')
            log.info(f'  Holding {HOLD_SECS}s...')
            time.sleep(HOLD_SECS)
            executor.reset_nonce()
            close_txh = fl.close_borrow(encoded, p)
            log.info(f'  CLOSE OK  tx={close_txh[:22]}...')

        elif borrow_type == 'compound_borrow':
            avail = cb.check_availability(
                Web3.to_checksum_address(p['comet_address']),
                float(p.get('max_utilization', 0.90))
            )
            if not avail['available']:
                log.warning(f'  SKIP — util={avail["utilization"]:.1%}')
                return 'SKIP', 0.0
            encoded, txh = cb.open_borrow(p)
            log.info(f'  OPEN OK  tx={txh[:22]}...')
            hf = cb.check_health(encoded, p)
            log.info(f'  Health: {hf:.2f}x')
            log.info(f'  Holding {HOLD_SECS}s...')
            time.sleep(HOLD_SECS)
            executor.reset_nonce()
            close_txh = cb.close_borrow(encoded, p)
            log.info(f'  CLOSE OK  tx={close_txh[:22]}...')

        else:
            log.error(f'  Unknown borrow_type: {borrow_type}')
            return 'FAIL', 0.0

    except Exception as e:
        log.error(f'  BORROW step FAIL: {e}')
        return 'FAIL', 0.0

    delta = eth_bal() - eth_before
    log.info(f'  Borrow delta: {delta:+.6f} ETH')
    return 'PASS', delta


# ── Lend step ─────────────────────────────────────────────────────────────────

def step_lend(platform_key: str) -> tuple:
    """Supply + withdraw via aave_supply. Returns (PASS|FAIL, eth_delta)."""
    p           = CFG['platforms'][platform_key]
    tok_sym     = p['token']
    tok_cfg     = CFG['tokens'][tok_sym]
    tok_addr    = Web3.to_checksum_address(p['token_address'])
    atoken_addr = Web3.to_checksum_address(p['atoken_address'])
    amount_wei  = int(tok_cfg['position_amount'] * 10**tok_cfg['decimals'])

    log.info(f'  [aave_supply] {platform_key}  {tok_sym}  {tok_cfg["position_amount"]}')

    eth_before = eth_bal()

    try:
        executor.reset_nonce()
        # Acquire
        if tok_sym == 'WETH':
            swap.wrap_eth(amount_wei)
        else:
            swap.attempt_swap(swap.swap_eth_to_token, tok_addr, amount_wei)
        log.info(f'  ACQUIRE {tok_sym} OK')
        time.sleep(3)

        # Supply
        txh = aave_supply.supply(tok_addr, amount_wei)
        log.info(f'  SUPPLY OK  tx={txh[:22]}...')
        time.sleep(4)

        # Check balance
        bal = aave_supply.get_atoken_balance(atoken_addr)
        log.info(f'  aToken: {bal / 10**tok_cfg["decimals"]:.6f} {tok_sym}')

        log.info(f'  Holding {HOLD_SECS}s...')
        time.sleep(HOLD_SECS)

        # Withdraw
        executor.reset_nonce()
        txh2 = aave_supply.withdraw_all(tok_addr)
        log.info(f'  WITHDRAW OK  tx={txh2[:22]}...')
        time.sleep(4)

        # Convert back
        tok_c = executor.w3.eth.contract(address=tok_addr, abi=executor.ERC20_ABI)
        tok_bal = tok_c.functions.balanceOf(executor.WALLET).call()
        if tok_sym == 'WETH':
            swap.unwrap_all_weth()
        elif tok_bal > 0:
            swap.attempt_swap(swap.swap_token_to_eth, tok_addr, tok_bal)
        time.sleep(3)

    except Exception as e:
        log.error(f'  LEND step FAIL: {e}')
        return 'FAIL', 0.0

    delta = eth_bal() - eth_before
    log.info(f'  Lend delta: {delta:+.6f} ETH')
    return 'PASS', delta


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    state.init_db()

    log.info('=' * 70)
    log.info('TEST_MIX_DEMO — 5 rounds x (LP + Borrow + Lend)')
    log.info(f'DRY_RUN={DRY_RUN}  Wallet={executor.WALLET}')
    log.info(f'Log: {_log_file}')
    log.info('=' * 70)

    total_eth_start = eth_bal()
    log.info(f'ETH balance start: {total_eth_start:.6f}')

    results = []  # list of (round_num, lp_status, borrow_status, lend_status, round_delta)

    for i, (lp_key, lp_type, borrow_key, borrow_type, lend_key) in enumerate(ROUNDS, 1):
        log.info(f'\n{"=" * 70}')
        log.info(f'ROUND {i}/5')
        log.info(f'  LP     : {lp_key}')
        log.info(f'  Borrow : {borrow_key}')
        log.info(f'  Lend   : {lend_key}')
        log.info('=' * 70)

        round_eth_start = eth_bal()
        log.info(f'  Round ETH start: {round_eth_start:.6f}')

        # ── Step A: LP ────────────────────────────────────────────────────────
        log.info(f'\n  --- STEP A: LP ---')
        lp_status, lp_delta = step_lp(lp_key, lp_type)
        log.info(f'  LP result: {lp_status}  delta={lp_delta:+.6f} ETH')

        # ── Step B: Borrow ────────────────────────────────────────────────────
        log.info(f'\n  --- STEP B: Borrow ---')
        borrow_status, borrow_delta = step_borrow(borrow_key, borrow_type)
        log.info(f'  Borrow result: {borrow_status}  delta={borrow_delta:+.6f} ETH')

        # ── Step C: Lend ──────────────────────────────────────────────────────
        log.info(f'\n  --- STEP C: Lend ---')
        lend_status, lend_delta = step_lend(lend_key)
        log.info(f'  Lend result: {lend_status}  delta={lend_delta:+.6f} ETH')

        # ── Round summary ─────────────────────────────────────────────────────
        round_delta = eth_bal() - round_eth_start
        eth_price   = executor.get_token_usd_price('WETH')
        round_pass  = all(s in ('PASS', 'SKIP') for s in [lp_status, borrow_status, lend_status])

        log.info(f'\n  Round {i} RESULT: {"PASS" if round_pass else "FAIL"}')
        log.info(f'  LP={lp_status}  Borrow={borrow_status}  Lend={lend_status}')
        log.info(f'  Round delta: {round_delta:+.6f} ETH  (${round_delta * eth_price:+.2f})')

        results.append((i, lp_status, borrow_status, lend_status, round_delta))

        if i < len(ROUNDS):
            log.info(f'\n  Sleeping 30min before next round...')
            time.sleep(1800)

    # ── Final summary ─────────────────────────────────────────────────────────
    total_delta   = eth_bal() - total_eth_start
    eth_price_fin = executor.get_token_usd_price('WETH')
    pass_count    = sum(
        1 for _, lp, borrow, lend, _ in results
        if all(s in ('PASS', 'SKIP') for s in [lp, borrow, lend])
    )

    log.info(f'\n{"=" * 70}')
    log.info('FINAL SUMMARY — test_mix_demo')
    log.info(f'{"=" * 70}')
    log.info(f'  {"RND":<4} {"LP":<8} {"BORROW":<8} {"LEND":<8} {"DELTA ETH":>14}')
    log.info(f'  {"-"*4} {"-"*8} {"-"*8} {"-"*8} {"-"*14}')
    for rnd, lp, borrow, lend, delta in results:
        log.info(f'  {rnd:<4} {lp:<8} {borrow:<8} {lend:<8} {delta:>+14.6f}')
    log.info(f'  {"-"*4} {"-"*8} {"-"*8} {"-"*8} {"-"*14}')
    log.info(f'  TOTAL {"":<4} {"":<8} {"":<8} {total_delta:>+14.6f} ETH  (${total_delta * eth_price_fin:+.2f})')
    log.info(f'\n  {pass_count}/5 rounds PASS')
    log.info(f'  Log: {_log_file}')
    log.info('=' * 70)


if __name__ == '__main__':
    main()
