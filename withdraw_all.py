"""
withdraw_all.py — Withdraw ALL active positions from Base agent, convert everything to ETH.

Usage:
    python withdraw_all.py              # withdraw all active positions
    DRY_RUN=true python withdraw_all.py # simulate (no TX sent)

Log output:
    console  +  logs/withdraw_YYYYMMDD_HHMMSS.log

Processing order: position id ASC (oldest first).
Each position: withdraw from platform → convert token → ETH → close in state.db.

ETH delta per position and total are reported in the final summary table.
"""

import os, sys, json, logging, time
from datetime import datetime
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

import state, executor, swap
from swap import PriceGuardError, ConfigError, SwapExecutionError
import aave_supply as _aave_supply
import aave_borrow as _aave_borrow

# ── Logging ────────────────────────────────────────────────────────────────────

os.makedirs('logs', exist_ok=True)
_ts       = datetime.now().strftime('%Y%m%d_%H%M%S')
_log_file = f'logs/withdraw_{_ts}.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(_log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as f:
    CFG = json.load(f)

USDC_ADDR  = CFG['tokens']['USDC']['address']
USDS_ADDR  = CFG['tokens']['USDS']['address']
SUSDS_ADDR = CFG['tokens']['sUSDS']['address']

# Stablecoins with no Uniswap v3 pool — must convert via Aerodrome sAMM -> USDC -> ETH
AERO_STABLE_ONLY_TOKENS = {
    CFG['tokens']['DOLA']['address'].lower(),
    CFG['tokens']['USDz']['address'].lower(),
}

# Priority ordering: Borrow=0 (close first to stop debt accrual) → Supply=1 → LP=2 → Other=3
_BORROW_TYPES = {'compound_borrow', 'mw_borrow', 'fluid_borrow', 'aave_borrow'}
_SUPPLY_TYPES = {'comet', 'erc4626', 'ctoken', 'psm_hold', 'beefy_single', 'aave_supply'}
_LP_TYPES     = {'beefy_lp', 'aero_lp', 'uni_lp', 'pancake_lp'}
# Types not in any set above (megapot, deploy_contract, aero_vote, unknown) → priority 3 (Other, last)
# These do not accrue debt and can be handled after supply/lend/LP positions.


def _type_priority(pos_row: tuple) -> tuple:
    """Sort key: (priority_group, pos_id). Lower group = withdraw first."""
    ptype = CFG['platforms'].get(pos_row[1], {}).get('type', '')
    if ptype in _BORROW_TYPES: return (0, pos_row[0])
    if ptype in _SUPPLY_TYPES: return (1, pos_row[0])
    if ptype in _LP_TYPES:     return (2, pos_row[0])
    return (3, pos_row[0])

w3     = executor.w3
WALLET = executor.WALLET

# ── Helpers ────────────────────────────────────────────────────────────────────

def _tok_addr(platform_key: str) -> str:
    p = CFG['platforms'][platform_key]
    return p.get('token_address', USDC_ADDR)

def _tok_decimals(token: str) -> int:
    return CFG['tokens'].get(token, {}).get('decimals', 18)

def _read_token_balance_wei(tok_addr: str) -> int:
    for _attempt in range(3):
        try:
            c = w3.eth.contract(address=Web3.to_checksum_address(tok_addr), abi=executor.ERC20_ABI)
            return c.functions.balanceOf(WALLET).call()
        except Exception as _e:
            if '429' in str(_e) and _attempt < 2:
                time.sleep(5)
                continue
            raise

def _rpc_call(fn, *args, retries: int = 3, base_delay: int = 5):
    """Call fn(*args) with exponential backoff on 429 rate-limit errors.
    Waits: 5s → 15s → 45s (base_delay * 3^attempt)."""
    for _attempt in range(retries):
        try:
            return fn(*args)
        except Exception as _e:
            if '429' in str(_e) and _attempt < retries - 1:
                wait = base_delay * (3 ** _attempt)
                log.warning(f'RPC 429 — retry {_attempt+1}/{retries-1} in {wait}s')
                time.sleep(wait)
                continue
            raise

def _eth_wei() -> int:
    return _rpc_call(w3.eth.get_balance, WALLET)

# ── Step 1: Withdraw from platform ─────────────────────────────────────────────

def _platform_withdraw(platform_key: str, amount_wei: int, pos_id: int = None) -> tuple:
    """
    Withdraw from platform. Returns (tx_label, received_wei, received_token).

    For psm_hold (sUSDS): sells all sUSDS → USDC via PSM3.
                          received_wei = USDC wei received.
                          received_token = 'USDC' (not sUSDS).
    For all others:       received_wei = actual underlying token received.
                          received_token = platform's token.
    """
    p        = CFG['platforms'][platform_key]
    addr     = p.get('address')                              # borrow-type platforms have no plain 'address'
    token    = p.get('token') or p.get('borrow_token', '')  # borrow configs use borrow_token
    t        = p['type']
    tok_addr = _tok_addr(platform_key)

    # Snapshot balance before to compute actual received
    if token == 'sUSDS':
        bal_before = _read_token_balance_wei(USDC_ADDR)   # sUSDS → USDC, measure USDC
        received_token = 'USDC'
    elif token == 'ETH':
        bal_before = _eth_wei()
        received_token = 'ETH'
    else:
        bal_before = _read_token_balance_wei(tok_addr)
        received_token = token

    if t == 'comet':
        txh = executor.compound_withdraw(addr, tok_addr, amount_wei)
    elif t == 'erc4626':
        txh = executor.erc4626_withdraw_all(addr)
    elif t == 'ctoken':
        txh = executor.ctoken_withdraw_all(addr)
    elif t == 'psm_hold':
        usdc_received = executor.psm_swap_susds_to_usdc(amount_wei)
        return 'psm_susds_to_usdc', usdc_received, 'USDC'
    elif t == 'beefy_single':
        txh = executor.beefy_withdraw_all(addr)
    elif t == 'beefy_lp':
        # Step 1: Beefy withdraw → LP token in wallet
        txh = executor.beefy_withdraw_all(addr)
        time.sleep(4)
        lp_addr = executor.Web3.to_checksum_address(p['lp_address'])
        lp_c    = executor.w3.eth.contract(address=lp_addr, abi=executor.ERC20_ABI)
        lp_bal  = _rpc_call(lp_c.functions.balanceOf(executor.WALLET).call)
        log.info(f'  LP in wallet after beefy_withdraw: {lp_bal}')
        if lp_bal == 0:
            return txh, 0, 'LP'
        # Step 2: removeLiquidity → token0 + token1 in wallet
        t0_addr = executor.Web3.to_checksum_address(p['token0_address'])
        t1_addr = executor.Web3.to_checksum_address(p['token1_address'])
        stable  = p.get('stable', False)
        executor.aerodrome_remove_liquidity(t0_addr, t1_addr, stable, lp_bal)
        return txh, lp_bal, 'LP'
    elif t == 'aero_lp':
        gauge_addr = executor.Web3.to_checksum_address(p['gauge_address'])
        pool_addr  = executor.Web3.to_checksum_address(p['pool_address'])
        gauge_c    = executor.w3.eth.contract(address=gauge_addr, abi=executor.GAUGE_ABI)
        staked     = _rpc_call(gauge_c.functions.balanceOf(executor.WALLET).call)
        log.info(f'  gauge staked LP: {staked}')
        # Claim rewards first (best-effort)
        try:
            executor.aerodrome_gauge_claim(gauge_addr)
        except Exception as e:
            log.warning(f'  gauge_claim skipped: {e}')
        if staked == 0:
            return 'aero_lp_no_staked', 0, 'LP'
        # Unstake LP from gauge
        txh = executor.aerodrome_gauge_unstake(gauge_addr, staked)
        time.sleep(4)
        lp_c   = executor.w3.eth.contract(address=pool_addr, abi=executor.ERC20_ABI)
        lp_bal = _rpc_call(lp_c.functions.balanceOf(executor.WALLET).call)
        log.info(f'  LP in wallet after unstake: {lp_bal}')
        if lp_bal == 0:
            return txh, 0, 'LP'
        # Remove liquidity → token0 + token1
        t0_addr = executor.Web3.to_checksum_address(p['token0_address'])
        t1_addr = executor.Web3.to_checksum_address(p['token1_address'])
        stable  = p.get('stable', False)
        executor.aerodrome_remove_liquidity(t0_addr, t1_addr, stable, lp_bal)
        time.sleep(4)  # RPC eventual consistency — balances not readable immediately
        return txh, lp_bal, 'LP'
    elif t == 'uni_lp':
        from uni_lp import close_uni_lp
        # amount_wei = tokenId (int) stored in state.db
        txh = close_uni_lp(amount_wei)
        time.sleep(4)  # wait for RPC to reflect token balances
        return txh, amount_wei, 'LP'
    elif t == 'pancake_lp':
        from pancake_lp import close_pancake_lp
        txh = close_pancake_lp(amount_wei)
        time.sleep(4)
        return txh, amount_wei, 'LP'
    elif t == 'mw_borrow':
        import moonwell_borrow as _mw
        # close_borrow handles: repay debt + redeem collateral + convert all to ETH + close DB pos
        _mw.close_borrow(str(amount_wei), p, pos_id=pos_id)
        return 'mw_borrow_handled', 0, 'ETH'
    elif t == 'compound_borrow':
        import compound_borrow as _cb
        # close_borrow handles: repay debt + withdraw all collateral + convert to ETH
        txh = _cb.close_borrow(str(amount_wei), p)
        return txh, 0, 'ETH'
    elif t == 'fluid_borrow':
        import fluid_borrow as _fl
        # close_borrow: operate(nftId, INT256_MIN, INT256_MIN) — repay all + withdraw all + ETH
        txh = _fl.close_borrow(str(amount_wei), p)
        return txh, 0, 'ETH'
    elif t == 'aave_supply':
        txh = _aave_supply.withdraw_all(p['token_address'])
        time.sleep(4)
        # Read actual balance — aToken accrues interest, actual > DB-stored amount_wei
        actual_received = _read_token_balance_wei(tok_addr) if token != 'ETH' else _eth_wei()
        return txh, actual_received, p['token']
    elif t == 'aave_borrow':
        txh = _aave_borrow.close_borrow(str(amount_wei), p)
        time.sleep(4)
        return txh, 0, 'ETH'  # ETH delta measured externally via _eth_wei()
    elif t == 'aero_vote':
        from aero_vote import aero_vote_exit, VE_ADDR, VE_ABI
        # amount_wei_str format: "tokenId|aeroWei" (stored as string, not parsed as int)
        token_id = int(str(amount_wei).split('|')[0])
        # Check lock on-chain first — raises LOCKED_SKIP if still locked
        ve = executor.w3.eth.contract(
            address=executor.Web3.to_checksum_address(VE_ADDR), abi=VE_ABI
        )
        locked_info = _rpc_call(ve.functions.locked(token_id).call)
        lock_end    = locked_info[1]
        now = _rpc_call(executor.w3.eth.get_block, 'latest')['timestamp']
        if lock_end > now:
            remaining_h = (lock_end - now) // 3600
            raise RuntimeError(f'LOCKED_SKIP: veAERO tokenId={token_id} expires in {remaining_h}h')
        txh = aero_vote_exit(token_id)
        # aero_vote_exit handles reset + unlock + AERO->USDC->ETH internally
        return txh, 0, 'ETH'
    else:
        raise ValueError(f'Unknown platform type: {t}')

    # Wait for RPC node to sync state after TX confirm (public RPC eventual consistency)
    time.sleep(4)

    # Snapshot after — compute actual received
    if token == 'ETH':
        bal_after = _eth_wei()
    else:
        bal_after = _read_token_balance_wei(tok_addr)

    received = max(bal_after - bal_before, 0)
    return txh, received, received_token

# ── Step 2: Convert token → ETH ───────────────────────────────────────────────

def _to_eth(platform_key: str, received_wei: int, received_token: str) -> str:
    """
    Convert received token → native ETH.
    received_wei  = actual token amount in wallet after withdraw.
    received_token = token symbol (may differ from platform token for psm_hold).
    Returns: tx_hash string or descriptive label (no-op cases).
    """
    p     = CFG['platforms'][platform_key]
    token = p.get('token', '')   # uni_lp/pancake_lp have no plain 'token' key
    tok_addr = _tok_addr(platform_key)

    ptype = p['type']

    if token == 'ETH':
        return 'native_eth_no_action'

    # aero_vote / compound_borrow / fluid_borrow / mw_borrow: full conversion done inside _platform_withdraw
    if ptype in ('aero_vote', 'compound_borrow', 'fluid_borrow', 'mw_borrow'):
        return f'{ptype}_handled'

    # beefy_lp / aero_lp / uni_lp / pancake_lp: liquidity already removed — token0+token1 are in wallet
    if ptype in ('beefy_lp', 'aero_lp', 'uni_lp', 'pancake_lp'):
        t0_addr = executor.Web3.to_checksum_address(p['token0_address'])
        t1_addr = executor.Web3.to_checksum_address(p['token1_address'])
        t0_sym  = p['token0']
        t1_sym  = p['token1']
        results_txh = []
        for t_addr, t_sym in [(t0_addr, t0_sym), (t1_addr, t1_sym)]:
            t_bal = _read_token_balance_wei(t_addr)
            if t_bal == 0:
                continue
            if t_sym == 'WETH':
                swap.unwrap_all_weth()
                results_txh.append('unwrap_weth')
            elif t_addr.lower() in AERO_STABLE_ONLY_TOKENS:
                # No Uniswap v3 pool: token -> USDC via Aerodrome sAMM, then USDC -> ETH
                txh1 = executor.aerodrome_swap_stable(t_addr, USDC_ADDR, t_bal)
                results_txh.append(txh1)
                time.sleep(4)
                usdc_received = _read_token_balance_wei(USDC_ADDR)
                if usdc_received > 0:
                    txh2 = swap.attempt_swap(swap.swap_token_to_eth, USDC_ADDR, usdc_received)
                    results_txh.append(txh2)
            else:
                txh = swap.attempt_swap(swap.swap_token_to_eth, t_addr, t_bal)
                results_txh.append(txh)
        return ','.join(results_txh) if results_txh else 'lp_no_tokens'

    if token == 'WETH':
        swap.unwrap_all_weth()
        return 'unwrap_weth_done'

    if token == 'USDS':
        usdc = executor.psm_swap_usds_to_usdc(received_wei)
        return swap.attempt_swap(swap.swap_token_to_eth, USDC_ADDR, usdc)

    if token == 'sUSDS':
        actual_usdc = _read_token_balance_wei(USDC_ADDR)
        if actual_usdc == 0:
            return 'no_usdc_skip'
        return swap.attempt_swap(swap.swap_token_to_eth, USDC_ADDR, actual_usdc)

    # Generic ERC20: USDC, EURC, cbBTC, wstETH (incl. beefy_single)
    if received_wei == 0:
        log.warning(f'  Received 0 {token} - skipping swap')
        return 'zero_received_skip'
    return swap.attempt_swap(swap.swap_token_to_eth, tok_addr, received_wei)

# ── Main ────────────────────────────────────────────────────────────────────────

def _close_with_coplatform(pos_id: int, platform: str, all_positions: list,
                            closed_ids: set, dry: bool) -> None:
    """
    Close pos_id in DB, then close any other active positions for the same
    platform key that were not yet closed this run.

    Rationale: lend/supply/LP vaults are a single on-chain balance.  When we
    call withdraw_all / redeem(balanceOf) the entire vault position is emptied.
    Any duplicate DB rows for the same platform are now empty on-chain and must
    be closed too, otherwise they appear as active orphans with $0 value.
    """
    if pos_id not in closed_ids:
        if not dry:
            state.close_position(pos_id)
        closed_ids.add(pos_id)

    for other in all_positions:
        other_id  = other[0]
        other_plt = other[1]
        if other_plt == platform and other_id not in closed_ids:
            if not dry:
                state.close_position(other_id)
            closed_ids.add(other_id)
            log.info(f'  Co-platform #{other_id} ({platform}) also closed (shared on-chain balance)')


def run(ids=None):
    """
    ids: set/list of position IDs to process. None = all active.
    """
    state.init_db()

    all_positions = sorted(state.get_active(), key=_type_priority)  # Borrow -> Supply -> LP -> Other
    if ids is not None:
        id_set    = set(int(i) for i in ids)
        positions = [p for p in all_positions if p[0] in id_set]
    else:
        positions = all_positions

    # Track all DB IDs closed this run to avoid double-close.
    # When a vault is fully withdrawn on-chain, ALL DB entries for that platform
    # share the same on-chain balance — they must all be closed together.
    _closed_ids: set = set()

    if not positions:
        log.info('No active positions to withdraw.')
        return

    eth_start = _eth_wei()
    dry       = executor.DRY_RUN

    log.info('=' * 72)
    log.info(f'WITHDRAW ALL  {"[DRY RUN]" if dry else "[LIVE]"}')
    log.info(f'Wallet     : {WALLET}')
    log.info(f'Positions  : {len(positions)}')
    log.info(f'ETH start  : {Web3.from_wei(eth_start, "ether"):.6f}')
    log.info(f'Log file   : {_log_file}')
    log.info('=' * 72)

    results = []

    for pos in positions:
        pos_id, platform, token, amount_wei_str, entry, expiry, tx_supply, _status, *_rest = pos
        p_cfg   = CFG['platforms'].get(platform, {})
        p_type  = p_cfg.get('type', '')
        # aero_vote / compound_borrow / fluid_borrow / aave_borrow store encoded strings, not plain ints
        if p_type in ('aero_vote', 'compound_borrow', 'fluid_borrow', 'mw_borrow', 'aave_borrow'):
            amount_wei = amount_wei_str
        else:
            amount_wei = int(float(amount_wei_str))

        # Skip if already closed as a co-platform duplicate earlier this run
        if pos_id in _closed_ids:
            log.info(f'  [{pos_id}] {platform} already closed as co-platform duplicate — skip')
            continue

        log.info('')
        log.info(f'--- [{pos_id}] {platform} / {token}  entry={entry}  expiry={expiry} ---')

        try:
            import step_logger as _sl
            _sl.set_context(platform, p_cfg.get('display_name', platform))
        except Exception:
            pass

        eth_before = _eth_wei()
        result = dict(
            id=pos_id, platform=platform, token=token,
            status='PENDING', withdraw_tx=None, swap_tx=None,
            eth_before=eth_before, eth_after=eth_before, error=None,
        )

        # ── Step 1: Platform withdraw ──────────────────────────────────────────
        try:
            txh_w, received_wei, received_token = _platform_withdraw(platform, amount_wei, pos_id=pos_id)
            dec = _tok_decimals(received_token)
            log.info(f'  WITHDRAW OK  tx={txh_w}')
            log.info(f'               received={received_wei / 10**dec:.6f} {received_token}')
            result['withdraw_tx'] = txh_w
        except RuntimeError as e:
            if str(e).startswith('LOCKED_SKIP'):
                log.info(f'  SKIP (still locked): {e}')
                result['status'] = 'LOCKED_SKIP'
                results.append(result)
                continue
            log.error(f'  WITHDRAW FAILED: {e}')
            result['status'] = 'WITHDRAW_FAILED'
            result['error']  = str(e)
            results.append(result)
            continue
        except Exception as e:
            log.error(f'  WITHDRAW FAILED: {e}')
            result['status'] = 'WITHDRAW_FAILED'
            result['error']  = str(e)
            results.append(result)
            continue

        # mw_borrow: close_borrow handles ETH conversion + DB close internally — skip both steps
        if p_type == 'mw_borrow':
            _closed_ids.add(pos_id)
            # close_borrow already closed pos_id in DB; still close any co-platform duplicates
            for _other in all_positions:
                if _other[1] == platform and _other[0] not in _closed_ids:
                    if not dry:
                        state.close_position(_other[0])
                    _closed_ids.add(_other[0])
                    log.info(f'  Co-platform #{_other[0]} ({platform}) also closed (shared mToken)')
            result['eth_after'] = _eth_wei()
            result['status'] = 'OK'
            results.append(result)
            continue

        # aave_borrow already converted to ETH in close_borrow — skip token→ETH step
        if p_type == 'aave_borrow':
            _close_with_coplatform(pos_id, platform, all_positions, _closed_ids, dry)
            result['eth_after'] = _eth_wei()
            result['status'] = 'OK'
            results.append(result)
            continue

        # ── Step 2: Token → ETH ────────────────────────────────────────────────
        try:
            txh_s = _to_eth(platform, received_wei, received_token)
            log.info(f'  TO_ETH OK    tx={txh_s}')
            result['swap_tx'] = txh_s
        except (PriceGuardError, ConfigError) as e:
            log.warning(f'  TO_ETH SKIPPED (price/config guard): {e}')
            result['status'] = 'SWAP_SKIPPED'
            result['error']  = str(e)
            _close_with_coplatform(pos_id, platform, all_positions, _closed_ids, dry)
            result['eth_after'] = _eth_wei()
            results.append(result)
            continue
        except (SwapExecutionError, Exception) as e:
            log.error(f'  TO_ETH FAILED: {e}')
            result['status'] = 'SWAP_FAILED'
            result['error']  = str(e)
            _close_with_coplatform(pos_id, platform, all_positions, _closed_ids, dry)
            result['eth_after'] = _eth_wei()
            results.append(result)
            continue

        # ── Step 3: Close position in state.db ────────────────────────────────
        _close_with_coplatform(pos_id, platform, all_positions, _closed_ids, dry)
        result['eth_after'] = _eth_wei()
        delta = result['eth_after'] - eth_before
        log.info(f'  {"[DRY] skip close" if dry else "CLOSED in DB"}')
        log.info(f'  ETH delta: {delta / 1e18:+.6f}'
                 f'  (before={eth_before / 1e18:.6f}'
                 f'  after={result["eth_after"] / 1e18:.6f})')
        result['status'] = 'OK'
        results.append(result)

    # ── Final Summary ──────────────────────────────────────────────────────────
    eth_end   = _eth_wei()
    net_delta = eth_end - eth_start

    log.info('')
    log.info('=' * 72)
    log.info('WITHDRAW SUMMARY')
    log.info(f'  {"ID":>3}  {"Platform":22}  {"Token":6}  {"Status":15}  {"ETH Delta":>12}')
    log.info('  ' + '-' * 64)
    for r in results:
        delta     = r['eth_after'] - r['eth_before']
        delta_str = f'{delta / 1e18:+.6f}' if r['status'] != 'WITHDRAW_FAILED' else '      N/A'
        err_note  = f'  << {r["error"][:50]}' if r['error'] and r['status'] != 'OK' else ''
        log.info(f'  {r["id"]:>3}  {r["platform"]:22}  {r["token"]:6}  {r["status"]:15}  {delta_str:>12}{err_note}')
    log.info('  ' + '-' * 64)
    log.info(f'  {"":>3}  {"NET TOTAL":22}  {"":6}  {"":15}  {net_delta / 1e18:>+12.6f}')
    log.info('')
    log.info(f'  ETH before : {eth_start / 1e18:.6f}')
    log.info(f'  ETH after  : {eth_end / 1e18:.6f}')
    ok  = sum(1 for r in results if r['status'] == 'OK')
    bad = len(results) - ok
    log.info(f'  Positions  : {ok}/{len(results)} OK  {bad} failed/skipped')
    log.info(f'  Log file   : {_log_file}')
    log.info('=' * 72)


def force_close_all():
    """
    Emergency close — callable programmatically (e.g., from serve_dashboard.py API).
    Withdraws all active positions in priority order: Borrow -> Supply -> LP -> Other.
    """
    run()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Withdraw active positions (priority order: Borrow -> Supply -> LP -> Other)')
    parser.add_argument('--emergency', action='store_true',
                        help='Semantic flag for emergency logging — always processes all positions')
    parser.add_argument('--id', type=int, default=None, dest='pos_id',
                        help='Withdraw only this position ID (default: all active)')
    args = parser.parse_args()

    if args.emergency:
        log.warning('EMERGENCY CLOSE triggered via --emergency flag')

    if args.pos_id is not None:
        log.info(f'Single-position mode: withdrawing position #{args.pos_id}')
        run(ids={args.pos_id})
    else:
        run()
