"""
swap.py — ETH <-> Token swap module for base-agent.

Direction A (before action):  swap_eth_to_token(token_addr, amount_out_wei, decimals)
Direction B (after withdraw):  swap_token_to_eth(token_addr, amount_in_wei)

Quote engine: Uniswap v3 (fee 500 + 3000) + PancakeSwap v3 (fee 500) — picks best.
Price guard:  Chainlink ETH/USD for USD-stable tokens; quote-delta guard for others.
Retry:        attempt_swap(fn, *args) — transient errors retry x2, price/config fail immediately.
"""

import os, json, time, logging
from web3 import Web3
from dotenv import load_dotenv
import executor

load_dotenv()
log = logging.getLogger(__name__)

with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as f:
    CFG = json.load(f)

DEX       = CFG['dex']
WETH_ADDR = Web3.to_checksum_address(CFG['tokens']['WETH']['address'])
w3        = executor.w3
# NOTE: do NOT freeze the wallet address here. executor.WALLET changes when
# wallet_manager.switch_context() reloads executor for a different wallet.
# A frozen copy caused swaps to send bought tokens to the WRONG wallet
# (recipient stayed on the first-imported wallet while a switched wallet paid).
# Always read executor.WALLET live at call time. See ERRORS.md "swap misdelivery".

_SWAP_ADDR_SYM = {t['address'].lower(): s for s, t in CFG['tokens'].items()}


def _sym_for_addr(addr: str) -> str:
    return _SWAP_ADDR_SYM.get(addr.lower(), addr[:8] + '...')

# ── Custom exceptions ──────────────────────────────────────────────────────────

class PriceGuardError(Exception):
    """Price is too far from reference — skip, do not retry."""

class ConfigError(Exception):
    """Token not configured or balance insufficient — skip, do not retry."""

class SwapExecutionError(Exception):
    """Swap failed after retries."""

# ── ABIs ───────────────────────────────────────────────────────────────────────

WETH_ABI = [
    {"name": "deposit",  "type": "function", "stateMutability": "payable",
     "inputs": [], "outputs": []},
    {"name": "withdraw", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "wad", "type": "uint256"}], "outputs": []},
    {"name": "balanceOf","type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]

QUOTER_V2_ABI = [
    {
        "name": "quoteExactOutputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "params", "type": "tuple", "components": [
            {"name": "tokenIn",           "type": "address"},
            {"name": "tokenOut",          "type": "address"},
            {"name": "amount",            "type": "uint256"},
            {"name": "fee",               "type": "uint24"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ]}],
        "outputs": [
            {"name": "amountIn",                "type": "uint256"},
            {"name": "sqrtPriceX96After",       "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate",             "type": "uint256"},
        ],
    },
    {
        "name": "quoteExactInputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "params", "type": "tuple", "components": [
            {"name": "tokenIn",           "type": "address"},
            {"name": "tokenOut",          "type": "address"},
            {"name": "amountIn",          "type": "uint256"},
            {"name": "fee",               "type": "uint24"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ]}],
        "outputs": [
            {"name": "amountOut",               "type": "uint256"},
            {"name": "sqrtPriceX96After",       "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate",             "type": "uint256"},
        ],
    },
]

ROUTER_V3_ABI = [
    {
        "name": "exactOutputSingle",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "params", "type": "tuple", "components": [
            {"name": "tokenIn",           "type": "address"},
            {"name": "tokenOut",          "type": "address"},
            {"name": "fee",               "type": "uint24"},
            {"name": "recipient",         "type": "address"},
            {"name": "amountOut",         "type": "uint256"},
            {"name": "amountInMaximum",   "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ]}],
        "outputs": [{"name": "amountIn", "type": "uint256"}],
    },
    {
        "name": "exactInputSingle",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "params", "type": "tuple", "components": [
            {"name": "tokenIn",           "type": "address"},
            {"name": "tokenOut",          "type": "address"},
            {"name": "fee",               "type": "uint24"},
            {"name": "recipient",         "type": "address"},
            {"name": "amountIn",          "type": "uint256"},
            {"name": "amountOutMinimum",  "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ]}],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
]

# PancakeSwap v3 SmartRouter has deadline inside the params struct (unlike Uniswap SwapRouter02)
PANCAKE_ROUTER_V3_ABI = [
    {
        "name": "exactOutputSingle",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "params", "type": "tuple", "components": [
            {"name": "tokenIn",           "type": "address"},
            {"name": "tokenOut",          "type": "address"},
            {"name": "fee",               "type": "uint24"},
            {"name": "recipient",         "type": "address"},
            {"name": "deadline",          "type": "uint256"},
            {"name": "amountOut",         "type": "uint256"},
            {"name": "amountInMaximum",   "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ]}],
        "outputs": [{"name": "amountIn", "type": "uint256"}],
    },
    {
        "name": "exactInputSingle",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "params", "type": "tuple", "components": [
            {"name": "tokenIn",           "type": "address"},
            {"name": "tokenOut",          "type": "address"},
            {"name": "fee",               "type": "uint24"},
            {"name": "recipient",         "type": "address"},
            {"name": "deadline",          "type": "uint256"},
            {"name": "amountIn",          "type": "uint256"},
            {"name": "amountOutMinimum",  "type": "uint256"},
            {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ]}],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
]

CHAINLINK_ABI = [
    {"name": "latestRoundData", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [
         {"name": "roundId",         "type": "uint80"},
         {"name": "answer",          "type": "int256"},
         {"name": "startedAt",       "type": "uint256"},
         {"name": "updatedAt",       "type": "uint256"},
         {"name": "answeredInRound", "type": "uint80"},
     ]},
]

# ── Pool list: (name, quoter_addr, fee, router_addr, router_type) ─────────────

POOLS = [
    ('uniswap_500',   DEX['uniswap_quoter_v2'], 500,   DEX['uniswap_router'], 'uniswap'),
    ('uniswap_3000',  DEX['uniswap_quoter_v2'], 3000,  DEX['uniswap_router'], 'uniswap'),
    ('uniswap_10000', DEX['uniswap_quoter_v2'], 10000, DEX['uniswap_router'], 'uniswap'),
    ('pancake_500',   DEX['pancake_quoter_v2'], 500,   DEX['pancake_router'], 'pancake'),
    ('pancake_10000', DEX['pancake_quoter_v2'], 10000, DEX['pancake_router'], 'pancake'),
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def get_eth_usd_price() -> float:
    feed = w3.eth.contract(
        address=Web3.to_checksum_address(DEX['chainlink_eth_usd']),
        abi=CHAINLINK_ABI,
    )
    _, answer, _, _, _ = feed.functions.latestRoundData().call()
    return answer / 1e8


def _wrap_eth(amount_wei: int) -> None:
    weth = w3.eth.contract(address=WETH_ADDR, abi=WETH_ABI)
    tx = weth.functions.deposit().build_transaction(executor._tx_params(value=amount_wei))
    try:
        tx['gas'] = executor._gas_limit(tx)
    except Exception:
        tx['gas'] = 60_000
    executor._send(tx)
    log.info(f'Wrapped {Web3.from_wei(amount_wei, "ether"):.6f} ETH -> WETH')


def _unwrap_weth() -> None:
    import time
    weth = w3.eth.contract(address=WETH_ADDR, abi=WETH_ABI)
    executor.wait_for_sync()  # wait until read node reflects latest WETH balance (prior swap TX)
    bal = weth.functions.balanceOf(executor.WALLET).call()
    if bal == 0:
        return
    tx = weth.functions.withdraw(bal).build_transaction(executor._tx_params())
    try:
        tx['gas'] = executor._gas_limit(tx)
    except Exception:
        tx['gas'] = 60_000
    executor._send(tx)
    time.sleep(3)  # let pending nonce propagate before next TX
    log.info(f'Unwrapped {Web3.from_wei(bal, "ether"):.6f} WETH -> ETH')


def _quote_output(quoter_addr: str, token_out: str, amount_out: int, fee: int):
    """How much WETH needed to get exact amount_out. Returns int or None."""
    quoter = w3.eth.contract(address=Web3.to_checksum_address(quoter_addr), abi=QUOTER_V2_ABI)
    try:
        result = quoter.functions.quoteExactOutputSingle((
            WETH_ADDR,
            Web3.to_checksum_address(token_out),
            amount_out,
            fee,
            0,
        )).call()
        return result[0]
    except Exception:
        return None


def _quote_input(quoter_addr: str, token_in: str, amount_in: int, fee: int):
    """How much WETH received for exact amount_in. Returns int or None."""
    quoter = w3.eth.contract(address=Web3.to_checksum_address(quoter_addr), abi=QUOTER_V2_ABI)
    try:
        result = quoter.functions.quoteExactInputSingle((
            Web3.to_checksum_address(token_in),
            WETH_ADDR,
            amount_in,
            fee,
            0,
        )).call()
        return result[0]
    except Exception:
        return None


def _best_quote_for_output(token_out: str, amount_out: int) -> tuple:
    """Returns (pool_name, fee, router_addr, eth_cost_wei, router_type) — cheapest ETH."""
    results = []
    for name, quoter, fee, router, rtype in POOLS:
        cost = _quote_output(quoter, token_out, amount_out, fee)
        if cost is not None:
            log.debug(f'Quote [{name}] ETH cost: {Web3.from_wei(cost, "ether"):.6f}')
            results.append((name, fee, router, cost, rtype))
    if not results:
        raise SwapExecutionError(f'No pool liquidity for token {token_out}')
    return min(results, key=lambda x: x[3])


def _best_quote_for_input(token_in: str, amount_in: int) -> tuple:
    """Returns (pool_name, fee, router_addr, eth_out_wei, router_type) — most ETH."""
    results = []
    for name, quoter, fee, router, rtype in POOLS:
        out = _quote_input(quoter, token_in, amount_in, fee)
        if out is not None:
            log.debug(f'Quote [{name}] ETH out: {Web3.from_wei(out, "ether"):.6f}')
            results.append((name, fee, router, out, rtype))
    if not results:
        raise SwapExecutionError(f'No pool liquidity for token {token_in}')
    return max(results, key=lambda x: x[3])


def _is_stable_usd(token_addr: str) -> bool:
    for tok in CFG['tokens'].values():
        if tok['address'].lower() == token_addr.lower():
            return tok.get('is_stable_usd', False)
    return False


def _token_decimals(token_addr: str) -> int:
    for tok in CFG['tokens'].values():
        if tok['address'].lower() == token_addr.lower():
            return tok.get('decimals', 18)
    return 18


def _get_token_eth_expected(token_addr: str, token_amount_wei: int) -> int | None:
    """
    Returns expected ETH wei for token_amount_wei using the token's Chainlink feed.
    feed_type='eth_rate': feed returns ETH per token (e.g. wstETH/ETH).
    feed_type='usd':      feed returns USD per token; divide by ETH/USD for ETH.
    Returns None if token has no chainlink_feed configured.
    """
    addr_lower = token_addr.lower()
    for tok in CFG['tokens'].values():
        if tok['address'].lower() != addr_lower:
            continue
        feed_addr = tok.get('chainlink_feed')
        if not feed_addr:
            return None
        feed = w3.eth.contract(
            address=Web3.to_checksum_address(feed_addr), abi=CHAINLINK_ABI
        )
        _, answer, _, _, _ = feed.functions.latestRoundData().call()
        feed_decimals  = tok.get('feed_decimals', 8)
        token_decimals = tok.get('decimals', 18)
        rate           = answer / 10**feed_decimals
        token_human    = token_amount_wei / 10**token_decimals

        if tok['feed_type'] == 'eth_rate':
            return int(token_human * rate * 1e18)
        elif tok['feed_type'] == 'usd':
            eth_usd = get_eth_usd_price()
            return Web3.to_wei(token_human * rate / eth_usd, 'ether')
        return None
    return None

# ── Public wrap/unwrap (WETH platforms) ───────────────────────────────────────

def wrap_eth(amount_wei: int) -> None:
    """Wrap native ETH -> WETH. Use before supplying to WETH-based platforms."""
    eth_bal = w3.eth.get_balance(executor.WALLET)
    min_wei = Web3.to_wei(executor.MIN_ETH, 'ether')
    if eth_bal < amount_wei + min_wei:
        raise ConfigError(
            f'Insufficient ETH to wrap: need {Web3.from_wei(amount_wei + min_wei, "ether"):.5f}, '
            f'have {Web3.from_wei(eth_bal, "ether"):.5f}'
        )
    _wrap_eth(amount_wei)
    try:
        import step_logger as _sl
        _sl.slog('wrap', f'ETH → WETH  {Web3.from_wei(amount_wei, "ether"):.5f}')
    except Exception:
        pass


def unwrap_all_weth() -> None:
    """Unwrap all WETH → native ETH. Use after withdrawing from WETH-based platforms."""
    _unwrap_weth()
    try:
        import step_logger as _sl
        _sl.slog('unwrap', 'WETH → ETH')
    except Exception:
        pass


def unwrap_weth(amount_wei: int) -> None:
    """Unwrap exactly amount_wei WETH -> native ETH. Use for partial retention."""
    import time
    weth = w3.eth.contract(address=WETH_ADDR, abi=WETH_ABI)
    if amount_wei == 0:
        return
    tx = weth.functions.withdraw(amount_wei).build_transaction(executor._tx_params())
    try:
        tx['gas'] = executor._gas_limit(tx)
    except Exception:
        tx['gas'] = 60_000
    executor._send(tx)
    time.sleep(3)
    log.info(f'Unwrapped {Web3.from_wei(amount_wei, "ether"):.6f} WETH -> ETH (partial)')


# ── Public swap functions ──────────────────────────────────────────────────────

def swap_eth_to_token(token_out_addr: str, amount_out_wei: int) -> str:
    """
    Swap ETH → exact amount_out_wei of token_out.
    1. Guard ETH balance
    2. Chainlink price guard (stable tokens) or multi-pool comparison guard
    3. Wrap ETH -> WETH (quote + 2% buffer)
    4. exactOutputSingle on best DEX
    5. Unwrap leftover WETH -> ETH
    Returns tx hash.
    """
    token_out_addr = Web3.to_checksum_address(token_out_addr)

    # 1. Find best quote
    pool_name, fee, router_addr, eth_cost, router_type = _best_quote_for_output(token_out_addr, amount_out_wei)
    log.info(f'swap_eth_to_token | best: {pool_name} | ETH cost: {Web3.from_wei(eth_cost, "ether"):.6f}')

    # 2. Price guard
    amount_in_max = int(eth_cost * 1.02)
    expected_eth  = _get_token_eth_expected(token_out_addr, amount_out_wei)
    if expected_eth is not None:
        if eth_cost > int(expected_eth * 1.02):
            raise PriceGuardError(
                f'ETH cost {Web3.from_wei(eth_cost,"ether"):.6f} exceeds '
                f'Chainlink fair value {Web3.from_wei(expected_eth,"ether"):.6f} by >1%'
            )
        log.info(f'Chainlink guard OK | fair ETH: {Web3.from_wei(expected_eth,"ether"):.6f}')
    elif _is_stable_usd(token_out_addr):
        eth_price     = get_eth_usd_price()
        decimals      = _token_decimals(token_out_addr)
        token_usd_val = amount_out_wei / 10**decimals
        expected_eth  = Web3.to_wei(token_usd_val / eth_price, 'ether')
        if eth_cost > int(expected_eth * 1.02):
            raise PriceGuardError(
                f'ETH cost {Web3.from_wei(eth_cost,"ether"):.6f} exceeds '
                f'Chainlink fair value {Web3.from_wei(expected_eth,"ether"):.6f} by >1%'
            )
        log.info(f'Chainlink guard OK | ETH/USD: ${eth_price:.2f} | fair: {Web3.from_wei(expected_eth,"ether"):.6f}')

    # 3. ETH balance check
    eth_bal  = w3.eth.get_balance(executor.WALLET)
    min_wei  = Web3.to_wei(executor.MIN_ETH, 'ether')
    if eth_bal < amount_in_max + min_wei:
        raise ConfigError(
            f'Insufficient ETH: need {Web3.from_wei(amount_in_max + min_wei, "ether"):.5f}, '
            f'have {Web3.from_wei(eth_bal, "ether"):.5f}'
        )

    # 4. Wrap ETH -> WETH
    _wrap_eth(amount_in_max)

    # 5. Approve WETH → router
    executor._approve_if_needed(WETH_ADDR, router_addr, amount_in_max)

    # 6. exactOutputSingle
    router_abi = PANCAKE_ROUTER_V3_ABI if router_type == 'pancake' else ROUTER_V3_ABI
    router = w3.eth.contract(address=Web3.to_checksum_address(router_addr), abi=router_abi)
    deadline = w3.eth.get_block('latest')['timestamp'] + 300
    recipient = executor.WALLET   # MUST equal the signer — never a frozen copy
    out_token = w3.eth.contract(address=Web3.to_checksum_address(token_out_addr), abi=_BAL_ABI)
    bal_before = out_token.functions.balanceOf(recipient).call()
    if router_type == 'pancake':
        params = (WETH_ADDR, token_out_addr, fee, recipient, deadline, amount_out_wei, amount_in_max, 0)
    else:
        params = (WETH_ADDR, token_out_addr, fee, recipient, amount_out_wei, amount_in_max, 0)
    tx = router.functions.exactOutputSingle(params).build_transaction(executor._tx_params())
    tx['gas'] = executor._gas_limit(tx)
    txh = executor._send(tx)
    # TRIPWIRE: confirm the SIGNING wallet actually received the token. If a future
    # change ever re-introduces a stale/mismatched recipient, the bought token would
    # land in another wallet and this check fails loudly instead of silently draining.
    executor.wait_for_sync()
    bal_after = out_token.functions.balanceOf(recipient).call()
    received  = bal_after - bal_before
    if received < amount_out_wei * 9 // 10:
        raise SwapExecutionError(
            f'SWAP MISDELIVERY: {executor.WALLET} received {received} of {token_out_addr} '
            f'but expected ~{amount_out_wei} (recipient/signer mismatch?). Aborting before supply.'
        )
    try:
        import step_logger as _sl
        sym = _sym_for_addr(token_out_addr)
        _sl.slog('swap', f'ETH → {sym}  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    log.info(f'exactOutputSingle done: {txh} (received {received} {_sym_for_addr(token_out_addr)})')

    # 7. Unwrap leftover WETH (best-effort — swap already confirmed, cleanup failure != swap failure)
    try:
        _unwrap_weth()
    except Exception as unwrap_err:
        log.warning(f'_unwrap_weth cleanup failed after confirmed swap (WETH stays in wallet): {unwrap_err}')

    return txh


_DUST_ETH_WEI = Web3.to_wei(0.0001, 'ether')   # ~$0.35 at $3500/ETH — skip swaps smaller than this

_BAL_ABI = [{"name": "balanceOf", "type": "function", "stateMutability": "view",
             "inputs": [{"name": "", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]}]


def swap_token_to_eth(token_in_addr: str, amount_in_wei: int) -> str:
    """
    Swap exact amount_in_wei of token_in → ETH.
    0. Cap amount_in_wei to actual on-chain balance (prevents STF revert if state.db > withdrawn amount)
    1. Find best quote across pools; skip if output < dust threshold
    2. Price guard (stable: Chainlink; others: 1% from quote)
    3. Approve token → router
    4. exactInputSingle on best DEX
    5. Unwrap WETH -> ETH
    Returns tx hash.
    """
    token_in_addr = Web3.to_checksum_address(token_in_addr)

    # 0. Cap to actual balance — state.db amount may exceed what was received after protocol fees
    tok = w3.eth.contract(address=token_in_addr, abi=_BAL_ABI)
    actual_balance = tok.functions.balanceOf(executor.WALLET).call()
    if actual_balance == 0:
        raise ConfigError(f'swap_token_to_eth: no {_sym_for_addr(token_in_addr)} balance')
    if actual_balance < amount_in_wei:
        log.warning(f'swap_token_to_eth: balance {actual_balance} < requested {amount_in_wei} — capping to actual')
        amount_in_wei = actual_balance

    # 1. Best quote
    pool_name, fee, router_addr, eth_out, router_type = _best_quote_for_input(token_in_addr, amount_in_wei)
    log.info(f'swap_token_to_eth | best: {pool_name} | ETH out: {Web3.from_wei(eth_out, "ether"):.6f}')

    if eth_out < _DUST_ETH_WEI:
        raise ConfigError(
            f'swap_token_to_eth: output {Web3.from_wei(eth_out,"ether"):.6f} ETH below dust threshold — skipping'
        )

    # 2. Price guard
    expected_eth = _get_token_eth_expected(token_in_addr, amount_in_wei)
    if expected_eth is not None:
        if eth_out < int(expected_eth * 0.99):
            raise PriceGuardError(
                f'ETH out {Web3.from_wei(eth_out,"ether"):.6f} is below '
                f'Chainlink fair value {Web3.from_wei(expected_eth,"ether"):.6f} by >1%'
            )
        log.info(f'Chainlink guard OK | fair ETH: {Web3.from_wei(expected_eth,"ether"):.6f}')
    elif _is_stable_usd(token_in_addr):
        eth_price     = get_eth_usd_price()
        decimals      = _token_decimals(token_in_addr)
        token_usd_val = amount_in_wei / 10**decimals
        expected_eth  = Web3.to_wei(token_usd_val / eth_price, 'ether')
        if eth_out < int(expected_eth * 0.99):
            raise PriceGuardError(
                f'ETH out {Web3.from_wei(eth_out,"ether"):.6f} is below '
                f'Chainlink fair value {Web3.from_wei(expected_eth,"ether"):.6f} by >1%'
            )
        log.info(f'Chainlink guard OK | expected: {Web3.from_wei(expected_eth,"ether"):.6f}')

    amount_out_min = int(eth_out * 0.99)

    # 3. Approve token → router
    executor._approve_if_needed(token_in_addr, router_addr, amount_in_wei)

    # 4. exactInputSingle
    router_abi = PANCAKE_ROUTER_V3_ABI if router_type == 'pancake' else ROUTER_V3_ABI
    router = w3.eth.contract(address=Web3.to_checksum_address(router_addr), abi=router_abi)
    deadline = w3.eth.get_block('latest')['timestamp'] + 300
    if router_type == 'pancake':
        params = (token_in_addr, WETH_ADDR, fee, executor.WALLET, deadline, amount_in_wei, amount_out_min, 0)
    else:
        params = (token_in_addr, WETH_ADDR, fee, executor.WALLET, amount_in_wei, amount_out_min, 0)
    tx = router.functions.exactInputSingle(params).build_transaction(executor._tx_params())
    tx['gas'] = executor._gas_limit(tx)
    txh = executor._send(tx)
    try:
        import step_logger as _sl
        sym = _sym_for_addr(token_in_addr)
        _sl.slog('swap', f'{sym} → ETH  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    log.info(f'exactInputSingle done: {txh}')

    # 5. Unwrap WETH -> ETH
    _unwrap_weth()

    return txh

# ── Retry wrapper ──────────────────────────────────────────────────────────────

def attempt_swap(fn, *args, max_retries: int = 2, retry_delay: int = 30):
    """
    Call fn(*args) with retry logic.
    - PriceGuardError / ConfigError: raise immediately, no retry.
    - Other exceptions: retry up to max_retries times (wait retry_delay seconds).
    - Exhausted retries: raise SwapExecutionError.
    """
    for attempt in range(max_retries + 1):
        try:
            return fn(*args)
        except (PriceGuardError, ConfigError) as e:
            log.warning(f'No-retry error [{fn.__name__}]: {e}')
            raise
        except Exception as e:
            if attempt < max_retries:
                log.warning(f'Swap attempt {attempt + 1}/{max_retries + 1} failed: {e} — retry in {retry_delay}s')
                time.sleep(retry_delay)
            else:
                log.error(f'Swap failed after {max_retries + 1} attempts: {e}')
                raise SwapExecutionError(str(e)) from e
