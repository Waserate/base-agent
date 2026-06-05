"""
uni_lp.py — Uniswap v3 full-range LP positions on Base chain.

Strategy: full-range (MIN_TICK..MAX_TICK rounded to tickSpacing).
Budget:   $5 per pool split 50:50 USD (optimal for full-range positions).
Tracking: ERC-721 tokenId stored as amount_wei in state.db.

Usage (standalone test):
    DRY_RUN=true python uni_lp.py uni_lp_weth_usdc_3000
"""

import sys, time, logging
from web3 import Web3
import executor

log = logging.getLogger(__name__)

NFPM_ADDR = Web3.to_checksum_address('0x03a520b32c04bf3beef7beb72e919cf822ed34f1')

UNI_LP_BUDGET_USD = 5.0  # per pool — 50:50 USD split proven optimal for full-range v3

# Full-range tick bounds per fee tier (rounded to nearest tickSpacing)
FULL_RANGE_TICKS = {
    100:   (-887272, 887272),
    500:   (-887270, 887270),
    3000:  (-887220, 887220),
    10000: (-887200, 887200),
}

MAX_UINT128 = 2**128 - 1

# ── ABIs ───────────────────────────────────────────────────────────────────────

_POOL_FEE_ABI = [
    {"name": "fee", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint24"}]},
]

NFPM_ABI = [
    {
        "name": "mint", "type": "function", "stateMutability": "payable",
        "inputs": [{"name": "params", "type": "tuple", "components": [
            {"name": "token0",         "type": "address"},
            {"name": "token1",         "type": "address"},
            {"name": "fee",            "type": "uint24"},
            {"name": "tickLower",      "type": "int24"},
            {"name": "tickUpper",      "type": "int24"},
            {"name": "amount0Desired", "type": "uint256"},
            {"name": "amount1Desired", "type": "uint256"},
            {"name": "amount0Min",     "type": "uint256"},
            {"name": "amount1Min",     "type": "uint256"},
            {"name": "recipient",      "type": "address"},
            {"name": "deadline",       "type": "uint256"},
        ]}],
        "outputs": [
            {"name": "tokenId",   "type": "uint256"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "amount0",   "type": "uint256"},
            {"name": "amount1",   "type": "uint256"},
        ],
    },
    {
        "name": "positions", "type": "function", "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [
            {"name": "nonce",                    "type": "uint96"},
            {"name": "operator",                 "type": "address"},
            {"name": "token0",                   "type": "address"},
            {"name": "token1",                   "type": "address"},
            {"name": "fee",                      "type": "uint24"},
            {"name": "tickLower",                "type": "int24"},
            {"name": "tickUpper",                "type": "int24"},
            {"name": "liquidity",                "type": "uint128"},
            {"name": "feeGrowthInside0LastX128", "type": "uint256"},
            {"name": "feeGrowthInside1LastX128", "type": "uint256"},
            {"name": "tokensOwed0",              "type": "uint128"},
            {"name": "tokensOwed1",              "type": "uint128"},
        ],
    },
    {
        "name": "decreaseLiquidity", "type": "function", "stateMutability": "nonpayable",
        "inputs": [{"name": "params", "type": "tuple", "components": [
            {"name": "tokenId",   "type": "uint256"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "amount0Min","type": "uint256"},
            {"name": "amount1Min","type": "uint256"},
            {"name": "deadline",  "type": "uint256"},
        ]}],
        "outputs": [
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
    },
    {
        "name": "collect", "type": "function", "stateMutability": "nonpayable",
        "inputs": [{"name": "params", "type": "tuple", "components": [
            {"name": "tokenId",    "type": "uint256"},
            {"name": "recipient",  "type": "address"},
            {"name": "amount0Max", "type": "uint128"},
            {"name": "amount1Max", "type": "uint128"},
        ]}],
        "outputs": [
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
    },
]


# ── Helpers ────────────────────────────────────────────────────────────────────

_INCREASE_LIQ_TOPIC: str | None = None


def _increase_liq_topic() -> str:
    global _INCREASE_LIQ_TOPIC
    if _INCREASE_LIQ_TOPIC is None:
        h = Web3.keccak(text='IncreaseLiquidity(uint256,uint128,uint256,uint256)').hex()
        _INCREASE_LIQ_TOPIC = '0x' + h if not h.startswith('0x') else h
    return _INCREASE_LIQ_TOPIC


def _get_pool_fee(pool_addr: str) -> int:
    """Read fee() from Uniswap v3 pool contract."""
    pool = executor.w3.eth.contract(
        address=Web3.to_checksum_address(pool_addr), abi=_POOL_FEE_ABI
    )
    return pool.functions.fee().call()


def _extract_token_id(receipt) -> int:
    """Extract tokenId from NFPM mint receipt via IncreaseLiquidity(indexed tokenId) event."""
    topic = _increase_liq_topic()
    for entry in receipt.logs:
        if (entry.address.lower() == NFPM_ADDR.lower()
                and len(entry.topics) >= 2
                and ('0x' + entry.topics[0].hex()) == topic):
            return int.from_bytes(entry.topics[1], 'big')
    raise RuntimeError('uni_lp: IncreaseLiquidity event not found in mint receipt')


# ── Public API ─────────────────────────────────────────────────────────────────

def mint_uni_lp(pool_key: str) -> tuple:
    """
    Mint a full-range Uniswap v3 LP position for the given pool_key.

    Precondition: token0 and token1 must already be in wallet
                  (acquired by agent._prepare_token_safe).
    Returns: (tokenId: int, tx_hash: str)
    tokenId is stored as amount_wei in state.db for withdraw tracking.
    """
    executor._guard()
    cfg = executor._load_cfg()
    p   = cfg['platforms'][pool_key]

    t0_addr = Web3.to_checksum_address(p['token0_address'])
    t1_addr = Web3.to_checksum_address(p['token1_address'])
    t0_sym  = p['token0']
    t1_sym  = p['token1']

    # Uniswap v3 requires token0 < token1 by address
    if int(t0_addr, 16) > int(t1_addr, 16):
        t0_addr, t1_addr = t1_addr, t0_addr
        t0_sym,  t1_sym  = t1_sym,  t0_sym

    fee = _get_pool_fee(p['pool_address'])
    tick_lower, tick_upper = FULL_RANGE_TICKS.get(fee, (-887270, 887270))

    # Use actual wallet balance — acquired proportionally by _prepare_token_safe
    t0_c = executor.w3.eth.contract(address=t0_addr, abi=executor.ERC20_ABI)
    t1_c = executor.w3.eth.contract(address=t1_addr, abi=executor.ERC20_ABI)
    amt0 = t0_c.functions.balanceOf(executor.WALLET).call()
    amt1 = t1_c.functions.balanceOf(executor.WALLET).call()

    if executor.DRY_RUN and (amt0 == 0 or amt1 == 0):
        # DRY_RUN: swaps were skipped so wallet has no tokens — use price-based estimate
        cfg2    = executor._load_cfg()
        tokens  = cfg2['tokens']
        t0_dec  = tokens.get(t0_sym, {}).get('decimals', 18)
        t1_dec  = tokens.get(t1_sym, {}).get('decimals', 18)
        p0      = executor.get_token_usd_price(t0_sym)
        p1      = executor.get_token_usd_price(t1_sym)
        half    = UNI_LP_BUDGET_USD / 2
        amt0    = int(half / p0 * 10**t0_dec) if amt0 == 0 else amt0
        amt1    = int(half / p1 * 10**t1_dec) if amt1 == 0 else amt1
        log.info(f'[DRY RUN] using estimated amounts: t0={amt0} t1={amt1}')

    if amt0 == 0 or amt1 == 0:
        raise RuntimeError(
            f'uni_lp {pool_key}: zero wallet balance '
            f't0={t0_sym}:{amt0}  t1={t1_sym}:{amt1}'
        )

    log.info(
        f'uni_lp mint {pool_key}  fee={fee}  '
        f't0={t0_sym}:{amt0}  t1={t1_sym}:{amt1}  '
        f'ticks=[{tick_lower},{tick_upper}]'
    )

    deadline = executor.w3.eth.get_block('latest')['timestamp'] + 600

    executor._approve_if_needed(t0_addr, NFPM_ADDR, amt0)
    executor._approve_if_needed(t1_addr, NFPM_ADDR, amt1)

    if executor.DRY_RUN:
        log.info(f'[DRY RUN] SKIP uni_lp mint {pool_key}')
        return 0, '0x' + 'dd' * 32

    time.sleep(4)
    nfpm = executor.w3.eth.contract(address=NFPM_ADDR, abi=NFPM_ABI)
    tx = nfpm.functions.mint({
        'token0':         t0_addr,
        'token1':         t1_addr,
        'fee':            fee,
        'tickLower':      tick_lower,
        'tickUpper':      tick_upper,
        'amount0Desired': amt0,
        'amount1Desired': amt1,
        'amount0Min':     0,
        'amount1Min':     0,
        'recipient':      executor.WALLET,
        'deadline':       deadline,
    }).build_transaction(executor._tx_params())
    try:
        tx['gas'] = executor._gas_limit(tx)
    except Exception:
        tx['gas'] = 600_000
        log.warning(f'estimate_gas failed for uni_lp mint {pool_key} — fallback gas=600000')
    txh = executor._send(tx)

    time.sleep(4)
    receipt  = executor.w3.eth.get_transaction_receipt(txh)
    token_id = _extract_token_id(receipt)
    log.info(f'uni_lp minted  pool={pool_key}  tokenId={token_id}  tx={txh}')
    try:
        import step_logger as _sl
        _sl.slog('mint_lp', f'{t0_sym}/{t1_sym}  id={token_id}  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    return token_id, txh


def close_uni_lp(token_id: int) -> str:
    """
    Full position close: decreaseLiquidity(all liquidity) + collect(all tokens).
    Tokens land in wallet; caller is responsible for converting to ETH.
    Returns last tx_hash (collect).
    """
    executor._guard()

    if executor.DRY_RUN:
        log.info(f'[DRY RUN] SKIP uni_lp close tokenId={token_id}')
        return '0x' + 'dd' * 32

    nfpm = executor.w3.eth.contract(address=NFPM_ADDR, abi=NFPM_ABI)

    pos       = nfpm.functions.positions(token_id).call()
    liquidity = pos[7]  # index 7 = liquidity field

    if liquidity > 0:
        deadline = executor.w3.eth.get_block('latest')['timestamp'] + 600
        tx = nfpm.functions.decreaseLiquidity({
            'tokenId':    token_id,
            'liquidity':  liquidity,
            'amount0Min': 0,
            'amount1Min': 0,
            'deadline':   deadline,
        }).build_transaction(executor._tx_params())
        try:
            tx['gas'] = executor._gas_limit(tx)
        except Exception:
            tx['gas'] = 400_000
            log.warning(f'estimate_gas failed for decreaseLiquidity tokenId={token_id} — fallback')
        executor._send(tx)
        time.sleep(4)
    else:
        log.warning(f'uni_lp close: tokenId={token_id} liquidity=0 (already removed?)')

    # Collect all tokens + accumulated fees to wallet
    tx = nfpm.functions.collect({
        'tokenId':    token_id,
        'recipient':  executor.WALLET,
        'amount0Max': MAX_UINT128,
        'amount1Max': MAX_UINT128,
    }).build_transaction(executor._tx_params())
    try:
        tx['gas'] = executor._gas_limit(tx)
    except Exception:
        tx['gas'] = 300_000
        log.warning(f'estimate_gas failed for collect tokenId={token_id} — fallback')
    txh = executor._send(tx)
    log.info(f'uni_lp closed  tokenId={token_id}  collect_tx={txh}')
    return txh


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import os
    from dotenv import load_dotenv
    load_dotenv()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    pool_key = sys.argv[1] if len(sys.argv) > 1 else 'uni_lp_weth_usdc_3000'
    cfg = executor._load_cfg()
    p   = cfg['platforms'][pool_key]

    fee = _get_pool_fee(p['pool_address'])
    ticks = FULL_RANGE_TICKS.get(fee, (-887270, 887270))
    print(f'Pool: {pool_key}')
    print(f'  pool_addr : {p["pool_address"]}')
    print(f'  fee       : {fee}')
    print(f'  ticks     : {ticks}')
    print(f'  token0    : {p["token0"]} {p["token0_address"]}')
    print(f'  token1    : {p["token1"]} {p["token1_address"]}')
    print('Run with DRY_RUN=true to test mint flow.')
