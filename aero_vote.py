"""
Aerodrome veAERO lock + vote + withdraw (Phase 6)

Enter flow (aero_vote_enter):
  1. _pick_vote_pools()         -- discover top pools by emission score (cached 7d)
  2. swap ETH -> USDC           (Uniswap V3)
  3. swap USDC -> AERO          (Aerodrome V1 router)
  4. createLock(aero_bal, lock_seconds) -> tokenId
  5. vote(tokenId, pools[], weights[])   -- randomized weights

Exit flow (aero_vote_exit, only after lock_end):
  1. Voter.reset(tokenId)       -- clear vote state (required before withdraw)
  2. VotingEscrow.withdraw()    -- unlock AERO
  3. swap AERO -> USDC          (Aerodrome V1 router)
  4. swap USDC -> ETH           (Uniswap V3)

Pool discovery (_fetch_top_pools):
  - Voter.length() + Voter.pools(i) via Multicall3
  - Filter active gauges (isAlive)
  - Score: Gauge.rewardRate() / Gauge.totalSupply()
  - Cache to cache/vote_pools.json (TTL = 7 days / 1 epoch)

State storage: amount_wei field = "tokenId|aeroWei" (pipe-separated)
"""

import os, time, json, random, logging
from datetime import datetime, timezone, timedelta
from eth_abi import decode as abi_decode
from web3 import Web3
from dotenv import load_dotenv
from executor import (
    w3, WALLET, _guard, _tx_params, _gas_limit, _send, _approve_if_needed,
    ERC20_ABI, DRY_RUN,
)
from swap import swap_eth_to_token, swap_token_to_eth

load_dotenv()
log = logging.getLogger(__name__)

# Separate RPC for heavy discovery calls (Multicall3 batch) — Alchemy recommended
# Falls back to BASE_RPC_URL if not set
_disc_rpc = os.getenv('DISCOVERY_RPC_URL') or os.getenv('BASE_RPC_URL', 'https://mainnet.base.org')
w3_disc   = Web3(Web3.HTTPProvider(_disc_rpc))

# ── Addresses ──────────────────────────────────────────────────────────────────

AERO_ADDR        = Web3.to_checksum_address('0x940181a94A35A4569E4529A3CDfB74e38FD98631')
USDC_ADDR        = Web3.to_checksum_address('0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')
VE_ADDR          = Web3.to_checksum_address('0xeBf418Fe2512e7E6bd9b87a8F0f294aCDC67e6B4')
VOTER_ADDR       = Web3.to_checksum_address('0x16613524e02ad97eDfeF371bC883F2F5d6C480A5')
AERO_ROUTER      = Web3.to_checksum_address('0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43')
AERO_FACTORY     = Web3.to_checksum_address('0x420DD381b31aEf6683db6B902084cB0FFECe40Da')
MULTICALL3_ADDR  = Web3.to_checksum_address('0xcA11bde05977b3631167028862bE2a173976CA11')

WEEK             = 7 * 86400
CACHE_PATH       = os.path.join(os.path.dirname(__file__), 'cache', 'vote_pools.json')
CACHE_TTL_DAYS   = 7        # refresh once per epoch
BATCH_SIZE       = 100      # calls per Multicall3 request
MIN_TOTAL_SUPPLY = 1_000    # skip gauge with near-zero LP staked (wei)
TOP_N_CACHE      = 20       # how many top pools to cache
VOTE_SELECT_MIN  = 2        # min pools to vote on
VOTE_SELECT_MAX  = 5        # max pools to vote on

# Fallback pools used if discovery fails entirely
FALLBACK_POOLS = [
    Web3.to_checksum_address('0x6cDcb1C4A4D1C3C6d054b27AC5B77e89eAFb971d'),  # USDC/AERO vLP
    Web3.to_checksum_address('0xcDAC0d6c6C59727a65F871236188350531885C43'),  # WETH/USDC vLP
]

# ── ABIs ───────────────────────────────────────────────────────────────────────

VE_ABI = [
    {"name": "createLock", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "_value", "type": "uint256"}, {"name": "_lockDuration", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "withdraw", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "_tokenId", "type": "uint256"}],
     "outputs": []},
    {"name": "locked", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "_tokenId", "type": "uint256"}],
     "outputs": [{"name": "", "type": "tuple", "components": [
         {"name": "amount",      "type": "int128"},
         {"name": "end",         "type": "uint256"},
         {"name": "isPermanent", "type": "bool"},
     ]}]},
    {"name": "voted", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "_tokenId", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

VOTER_ABI = [
    {"name": "vote", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "_tokenId",  "type": "uint256"},
         {"name": "_poolVote", "type": "address[]"},
         {"name": "_weights",  "type": "uint256[]"},
     ],
     "outputs": []},
    {"name": "reset", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "_tokenId", "type": "uint256"}],
     "outputs": []},
    {"name": "length", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "pools", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "uint256"}],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "gauges", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "isAlive", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "lastVoted", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]

GAUGE_ABI = [
    {"name": "rewardRate",   "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "totalSupply",  "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
]

MULTICALL3_ABI = [
    {"name": "aggregate", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "calls", "type": "tuple[]", "components": [
         {"name": "target",   "type": "address"},
         {"name": "callData", "type": "bytes"},
     ]}],
     "outputs": [
         {"name": "blockNumber", "type": "uint256"},
         {"name": "returnData",  "type": "bytes[]"},
     ]},
    {"name": "tryAggregate", "type": "function", "stateMutability": "view",
     "inputs": [
         {"name": "requireSuccess", "type": "bool"},
         {"name": "calls", "type": "tuple[]", "components": [
             {"name": "target",   "type": "address"},
             {"name": "callData", "type": "bytes"},
         ]},
     ],
     "outputs": [{"name": "", "type": "tuple[]", "components": [
         {"name": "success",    "type": "bool"},
         {"name": "returnData", "type": "bytes"},
     ]}]},
]

AERO_ROUTER_ABI = [
    {"name": "swapExactTokensForTokens", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "amountIn",     "type": "uint256"},
         {"name": "amountOutMin", "type": "uint256"},
         {"name": "routes",       "type": "tuple[]", "components": [
             {"name": "from",    "type": "address"},
             {"name": "to",      "type": "address"},
             {"name": "stable",  "type": "bool"},
             {"name": "factory", "type": "address"},
         ]},
         {"name": "to",           "type": "address"},
         {"name": "deadline",     "type": "uint256"},
     ],
     "outputs": [{"name": "", "type": "uint256[]"}]},
    {"name": "getAmountsOut", "type": "function", "stateMutability": "view",
     "inputs": [
         {"name": "amountIn", "type": "uint256"},
         {"name": "routes",   "type": "tuple[]", "components": [
             {"name": "from",    "type": "address"},
             {"name": "to",      "type": "address"},
             {"name": "stable",  "type": "bool"},
             {"name": "factory", "type": "address"},
         ]},
     ],
     "outputs": [{"name": "", "type": "uint256[]"}]},
]

# ── Multicall3 helpers ─────────────────────────────────────────────────────────

def _multicall_batch(calls: list, allow_fail: bool = False) -> list:
    """
    Run a batch of (target_addr, calldata_bytes) via Multicall3.
    allow_fail=False: uses aggregate() — any failed call reverts entire batch.
    allow_fail=True:  uses tryAggregate(False) — failed calls return b'' instead of reverting.
    Returns list of raw bytes (empty bytes for failed calls when allow_fail=True).
    """
    mc = w3_disc.eth.contract(address=MULTICALL3_ADDR, abi=MULTICALL3_ABI)
    results = []
    for start in range(0, len(calls), BATCH_SIZE):
        chunk = calls[start:start + BATCH_SIZE]
        if allow_fail:
            raw = mc.functions.tryAggregate(False, chunk).call()
            results.extend(r[1] if r[0] else b'' for r in raw)
        else:
            _, return_data = mc.functions.aggregate(chunk).call()
            results.extend(return_data)
        time.sleep(0.5)  # avoid 429 on public RPC — no rush, runs once per epoch
    return results


def _encode(contract, fn_name: str, args: list = None) -> bytes:
    """Encode ABI calldata for a contract function call."""
    fn = contract.get_function_by_name(fn_name)
    return fn(*(args or [])).build_transaction({'gas': 0, 'gasPrice': 0, 'from': WALLET})['data']

# ── Pool discovery ─────────────────────────────────────────────────────────────

def _fetch_top_pools() -> list:
    """
    Enumerate ALL Aerodrome V2 pools via Voter, score by rewardRate/totalSupply,
    return top TOP_N_CACHE entries sorted by score descending.

    Each entry: {"pool": addr, "gauge": addr, "score": float,
                 "reward_rate": int, "total_supply": int}
    """
    voter = w3_disc.eth.contract(address=VOTER_ADDR, abi=VOTER_ABI)

    # Step 1: total pool count
    n = voter.functions.length().call()
    log.info(f'[pool_discovery] Voter.length()={n}')

    # Step 2: fetch all pool addresses
    calls = [
        (VOTER_ADDR, _encode(voter, 'pools', [i]))
        for i in range(n)
    ]
    raw = _multicall_batch(calls)
    all_pools = [
        Web3.to_checksum_address(abi_decode(['address'], r)[0])
        for r in raw
    ]

    # Step 3: fetch gauge address per pool
    calls = [(VOTER_ADDR, _encode(voter, 'gauges', [p])) for p in all_pools]
    raw = _multicall_batch(calls)
    all_gauges = [
        Web3.to_checksum_address(abi_decode(['address'], r)[0])
        for r in raw
    ]

    ZERO = '0x' + '0' * 40
    pool_gauge_pairs = [
        (p, g) for p, g in zip(all_pools, all_gauges)
        if g.lower() != ZERO
    ]
    log.info(f'[pool_discovery] pools with gauge: {len(pool_gauge_pairs)}/{n}')

    # Step 4: filter alive gauges
    calls = [(VOTER_ADDR, _encode(voter, 'isAlive', [g])) for _, g in pool_gauge_pairs]
    raw = _multicall_batch(calls)
    alive_flags = [abi_decode(['bool'], r)[0] for r in raw]
    alive_pairs = [
        pg for pg, alive in zip(pool_gauge_pairs, alive_flags) if alive
    ]
    log.info(f'[pool_discovery] alive gauges: {len(alive_pairs)}')

    # Step 5: fetch rewardRate + totalSupply (interleaved per gauge)
    gauge_addrs = [g for _, g in alive_pairs]
    gauge_contract = w3.eth.contract(
        address=MULTICALL3_ADDR, abi=GAUGE_ABI  # address unused for encoding
    )
    interleaved_calls = []
    for g in gauge_addrs:
        # Use a temporary contract object just for encoding (address doesn't matter)
        gc = w3.eth.contract(
            address=Web3.to_checksum_address(g), abi=GAUGE_ABI
        )
        interleaved_calls.append((g, _encode(gc, 'rewardRate')))
        interleaved_calls.append((g, _encode(gc, 'totalSupply')))

    # allow_fail=True: gauges without rewardRate/totalSupply return b'' instead of reverting
    raw = _multicall_batch(interleaved_calls, allow_fail=True)

    # Step 6: score and rank — skip gauges with empty return data
    scored = []
    for i, (pool, gauge) in enumerate(alive_pairs):
        rr_bytes = raw[i * 2]
        ts_bytes = raw[i * 2 + 1]
        if len(rr_bytes) < 32 or len(ts_bytes) < 32:
            continue  # gauge doesn't support rewardRate/totalSupply
        rr = abi_decode(['uint256'], rr_bytes)[0]
        ts = abi_decode(['uint256'], ts_bytes)[0]
        if ts < MIN_TOTAL_SUPPLY:
            continue
        score = rr / ts if ts > 0 else 0.0
        scored.append({
            'pool':         pool,
            'gauge':        gauge,
            'score':        score,
            'reward_rate':  rr,
            'total_supply': ts,
        })

    scored.sort(key=lambda x: x['score'], reverse=True)
    top = scored[:TOP_N_CACHE]
    log.info(f'[pool_discovery] top {len(top)} pools by score (best={top[0]["score"]:.8f} worst={top[-1]["score"]:.8f})')
    return top


def _load_pool_cache() -> list | None:
    """
    Load cached top pools. Returns list if cache exists and is < CACHE_TTL_DAYS old.
    Returns None if cache is missing or stale.
    """
    if not os.path.exists(CACHE_PATH):
        return None
    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
        fetched_at = datetime.fromisoformat(data['fetched_at'])
        age_days = (datetime.now(timezone.utc) - fetched_at).days
        if age_days >= CACHE_TTL_DAYS:
            log.info(f'[pool_cache] stale ({age_days}d >= {CACHE_TTL_DAYS}d TTL) — refreshing')
            return None
        log.info(f'[pool_cache] hit ({age_days}d old, {len(data["top_pools"])} pools)')
        return data['top_pools']
    except Exception as e:
        log.warning(f'[pool_cache] read error: {e} — refreshing')
        return None


def _save_pool_cache(top_pools: list):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, 'w') as f:
        json.dump({
            'fetched_at': datetime.now(timezone.utc).isoformat(),
            'top_pools':  top_pools,
        }, f, indent=2)
    log.info(f'[pool_cache] saved {len(top_pools)} pools')


def _random_weights(n: int) -> list:
    """
    Generate n random weights (int) that look human:
    not perfectly even — one pool gets slightly more, others less.
    Sum is roughly 1000.
    """
    raw = [random.uniform(0.6, 1.4) for _ in range(n)]
    total = sum(raw)
    scaled = [max(50, int(r / total * 1000)) for r in raw]
    return scaled


def _pick_vote_pools() -> list:
    """
    Return list of (pool_addr, weight) pairs to vote on.
    Fetches fresh data if cache is stale.
    Falls back to FALLBACK_POOLS if discovery fails.
    """
    if DRY_RUN:
        # Use fallback in dry-run — no need for live RPC discovery
        n = random.randint(VOTE_SELECT_MIN, VOTE_SELECT_MAX)
        n = min(n, len(FALLBACK_POOLS))
        selected = random.sample(FALLBACK_POOLS, n)
        weights = _random_weights(len(selected))
        log.info(f'[DRY RUN] _pick_vote_pools: {len(selected)} pools (fallback)')
        return list(zip(selected, weights))

    top = _load_pool_cache()
    if top is None:
        try:
            log.info('[pool_discovery] cache miss — fetching from chain...')
            top = _fetch_top_pools()
            _save_pool_cache(top)
        except Exception as e:
            log.warning(f'[pool_discovery] fetch failed: {e} — using fallback pools')
            n = random.randint(VOTE_SELECT_MIN, VOTE_SELECT_MAX)
            n = min(n, len(FALLBACK_POOLS))
            selected = random.sample(FALLBACK_POOLS, n)
            weights = _random_weights(len(selected))
            return list(zip(selected, weights))

    n = random.randint(VOTE_SELECT_MIN, min(VOTE_SELECT_MAX, len(top)))
    selected = random.sample(top, n)
    weights  = _random_weights(len(selected))
    addrs    = [p['pool'] for p in selected]

    log.info(
        f'[pool_discovery] selected {len(addrs)} pools: '
        + ', '.join(f'{a[:8]}...(w={w})' for a, w in zip(addrs, weights))
    )
    return list(zip(addrs, weights))

# ── Aerodrome V1 router swap ───────────────────────────────────────────────────

def _aero_route(from_addr: str, to_addr: str) -> list:
    return [{"from": from_addr, "to": to_addr, "stable": False, "factory": AERO_FACTORY}]


def _aero_swap(from_addr: str, to_addr: str, amount_in_wei: int, slippage: float = 0.05) -> int:
    """Swap via Aerodrome V1 router. Returns actual amount_out received (wei)."""
    router = w3.eth.contract(address=AERO_ROUTER, abi=AERO_ROUTER_ABI)
    route  = _aero_route(from_addr, to_addr)

    try:
        amounts = router.functions.getAmountsOut(amount_in_wei, route).call()
        min_out = int(amounts[-1] * (1 - slippage))
    except Exception:
        min_out = 0
        log.warning('[_aero_swap] getAmountsOut failed — min_out=0')

    _approve_if_needed(from_addr, AERO_ROUTER, amount_in_wei)
    deadline = w3.eth.get_block('latest')['timestamp'] + 600

    to_c = w3.eth.contract(address=Web3.to_checksum_address(to_addr), abi=ERC20_ABI)
    bal_before = to_c.functions.balanceOf(WALLET).call()

    tx = router.functions.swapExactTokensForTokens(
        amount_in_wei, min_out, route, WALLET, deadline
    ).build_transaction(_tx_params())
    try:
        tx['gas'] = _gas_limit(tx)
    except Exception:
        tx['gas'] = 400_000
        log.warning('[_aero_swap] estimate_gas failed — fallback 400000')
    _send(tx)
    time.sleep(4)

    bal_after = to_c.functions.balanceOf(WALLET).call()
    received  = max(bal_after - bal_before, 0)
    log.info(f'[_aero_swap] in={amount_in_wei}  out={received}')
    return received

# ── Receipt parsing ────────────────────────────────────────────────────────────

def _parse_token_id_from_receipt(receipt, contract_addr: str) -> int:
    """Parse ERC721 Transfer(0x0 -> wallet) tokenId from createLock receipt."""
    TRANSFER_SIG = Web3.keccak(text='Transfer(address,address,uint256)').hex()
    for entry in receipt['logs']:
        if (entry['address'].lower() == contract_addr.lower() and
                len(entry['topics']) == 4 and
                entry['topics'][0].hex() == TRANSFER_SIG):
            return int(entry['topics'][3].hex(), 16)
    raise RuntimeError('Could not parse tokenId from createLock receipt logs')

# ── Public API ─────────────────────────────────────────────────────────────────

def aero_vote_enter(lock_days: int = 7) -> dict:
    """
    Discover best pools, buy AERO, lock veAERO, vote.

    lock_days >= 7 (veAERO minimum 1 epoch).
    Lock-end rounded UP to next WEEK boundary.

    Returns: {
        "token_id": int, "aero_wei": int, "lock_end": int,
        "tx_lock": str, "tx_vote": str,
        "voted_pools": [(addr, weight), ...]
    }
    """
    if lock_days < 7:
        raise ValueError(f'lock_days={lock_days} < 7 — minimum is 1 epoch')
    _guard()

    import step_logger as _sl
    _sl.set_context('aero_vote', 'Aerodrome veAERO')

    import json as _json, os as _os
    with open(_os.path.join(_os.path.dirname(__file__), 'config/contracts.json')) as f:
        cfg = _json.load(f)
    usdc_units = cfg['platforms']['aero_vote']['usdc_amount']
    usdc_wei   = int(usdc_units * 10**6)

    log.info(f'[aero_vote] Enter  lock_days={lock_days}  usdc_spend={usdc_units}')
    _sl.slog('start', f'enter lock_days={lock_days}  usdc=${usdc_units}')

    # Phase 1-2: Discover and select vote pools BEFORE spending ETH
    voted_pools = _pick_vote_pools()
    pool_addrs  = [p for p, _ in voted_pools]
    vote_weights = [w for _, w in voted_pools]
    log.info(f'[aero_vote] Will vote on {len(pool_addrs)} pools')

    # Step 1: ETH -> USDC
    if DRY_RUN:
        log.info(f'[DRY RUN] SKIP ETH->USDC  out={usdc_wei}')
        actual_aero = int(usdc_units * 1e18)
    else:
        swap_eth_to_token(USDC_ADDR, usdc_wei)
        time.sleep(4)

        # Step 2: USDC -> AERO (Aerodrome V1 router)
        usdc_c   = w3.eth.contract(address=USDC_ADDR, abi=ERC20_ABI)
        usdc_bal = usdc_c.functions.balanceOf(WALLET).call()
        if usdc_bal == 0:
            raise RuntimeError('No USDC after ETH->USDC swap')
        log.info(f'[aero_vote] USDC balance: {usdc_bal / 1e6:.4f}')
        actual_aero = _aero_swap(USDC_ADDR, AERO_ADDR, usdc_bal)
        if actual_aero == 0:
            raise RuntimeError('No AERO after USDC->AERO swap')

    _sl.slog('swap', f'USDC -> AERO  {actual_aero/1e18:.4f}')
    log.info(f'[aero_vote] AERO to lock: {actual_aero / 1e18:.4f}')

    # Step 3: Approve VotingEscrow
    _approve_if_needed(AERO_ADDR, VE_ADDR, actual_aero)

    # Step 4: createLock — round UP to next WEEK boundary
    now = int(time.time()) if DRY_RUN else w3.eth.get_block('latest')['timestamp']
    lock_end_target = ((now + lock_days * 86400) // WEEK + 1) * WEEK
    lock_seconds    = lock_end_target - now
    log.info(f'[aero_vote] lock_seconds={lock_seconds}  (~{lock_seconds/86400:.1f}d)')

    ve = w3.eth.contract(address=VE_ADDR, abi=VE_ABI)
    if DRY_RUN:
        log.info(f'[DRY RUN] SKIP createLock  amount={actual_aero}  secs={lock_seconds}')
        token_id    = 999999
        lock_end_ts = lock_end_target
        tx_lock     = '0x' + 'dd' * 32
    else:
        tx = ve.functions.createLock(actual_aero, lock_seconds).build_transaction(_tx_params())
        try:
            tx['gas'] = _gas_limit(tx)
        except Exception:
            tx['gas'] = 500_000
            log.warning('[aero_vote] createLock estimate_gas failed — fallback 500000')
        tx_lock = _send(tx)
        log.info(f'[aero_vote] createLock tx={tx_lock}')
        time.sleep(4)
        receipt     = w3.eth.get_transaction_receipt(tx_lock)
        token_id    = _parse_token_id_from_receipt(receipt, VE_ADDR)
        locked      = ve.functions.locked(token_id).call()
        lock_end_ts = locked[1]
        log.info(f'[aero_vote] tokenId={token_id}  lock_end={lock_end_ts}')

    _sl.slog('lock', f'tokenId={token_id}  TX {tx_lock[:10]}...', txhash=tx_lock)

    # Step 5: Vote
    voter = w3.eth.contract(address=VOTER_ADDR, abi=VOTER_ABI)
    if DRY_RUN:
        log.info(f'[DRY RUN] SKIP vote  tokenId={token_id}  pools={len(pool_addrs)}  weights={vote_weights}')
        tx_vote = '0x' + 'ee' * 32
    else:
        tx = voter.functions.vote(token_id, pool_addrs, vote_weights).build_transaction(_tx_params())
        try:
            tx['gas'] = _gas_limit(tx)
        except Exception:
            tx['gas'] = 600_000
        tx_vote = _send(tx)
        log.info(f'[aero_vote] vote tx={tx_vote}')

    _sl.slog('vote', f'{len(pool_addrs)} pools  TX {tx_vote[:10]}...', txhash=tx_vote)
    _sl.slog('ok', f'tokenId={token_id}  aero={actual_aero/1e18:.4f}')
    log.info(
        f'[aero_vote] ENTER DONE  tokenId={token_id}  '
        f'aero={actual_aero/1e18:.4f}  lock_end={lock_end_ts}  pools={len(pool_addrs)}'
    )
    return {
        'token_id':    token_id,
        'aero_wei':    actual_aero,
        'lock_end':    lock_end_ts,
        'tx_lock':     tx_lock,
        'tx_vote':     tx_vote,
        'voted_pools': voted_pools,
    }


def aero_revote(token_id: int) -> str | None:
    """
    Re-vote for existing locked tokenId in current Aerodrome epoch.
    Checks Voter.lastVoted(tokenId) on-chain — skips if already voted this epoch.
    Returns vote tx hash, or None if skipped.
    """
    _guard()
    import step_logger as _sl
    _sl.set_context('aero_vote', 'Aerodrome veAERO')
    voter = w3.eth.contract(address=VOTER_ADDR, abi=VOTER_ABI)

    if not DRY_RUN:
        now           = w3.eth.get_block('latest')['timestamp']
        current_epoch = now // WEEK
        last_voted    = voter.functions.lastVoted(token_id).call()
        last_epoch    = last_voted // WEEK
        if last_epoch >= current_epoch:
            log.info(f'[aero_revote] tokenId={token_id} already voted this epoch — skip')
            return None

    voted_pools  = _pick_vote_pools()
    pool_addrs   = [p for p, _ in voted_pools]
    vote_weights = [wt for _, wt in voted_pools]

    log.info(f'[aero_revote] tokenId={token_id}  pools={len(pool_addrs)}  weights={vote_weights}')

    if DRY_RUN:
        log.info(f'[DRY RUN] SKIP vote  tokenId={token_id}')
        return '0x' + 'ee' * 32

    tx = voter.functions.vote(token_id, pool_addrs, vote_weights).build_transaction(_tx_params())
    try:
        tx['gas'] = _gas_limit(tx)
    except Exception:
        tx['gas'] = 600_000
    txh = _send(tx)
    log.info(f'[aero_revote] done  tokenId={token_id}  tx={txh}')
    _sl.slog('vote', f'revote tokenId={token_id}  {len(pool_addrs)} pools  TX {txh[:10]}...', txhash=txh)
    return txh


def aero_vote_exit(token_id: int) -> str:
    """
    Reset vote + withdraw veAERO (only after lock_end) + sell AERO->USDC->ETH.

    Raises RuntimeError('LOCKED_SKIP: ...') if lock not expired — caller
    should treat as non-fatal skip.

    Returns last tx hash.
    """
    _guard()
    import step_logger as _sl
    _sl.set_context('aero_vote', 'Aerodrome veAERO')
    _sl.slog('start', f'exit tokenId={token_id}')
    if DRY_RUN:
        log.info(f'[DRY RUN] SKIP aero_vote_exit  tokenId={token_id}')
        return '0x' + 'dd' * 32

    ve    = w3.eth.contract(address=VE_ADDR,    abi=VE_ABI)
    voter = w3.eth.contract(address=VOTER_ADDR, abi=VOTER_ABI)

    # Verify lock expired
    locked   = ve.functions.locked(token_id).call()
    lock_end = locked[1]
    now      = w3.eth.get_block('latest')['timestamp']
    if lock_end > now:
        diff_h = (lock_end - now) // 3600
        raise RuntimeError(f'LOCKED_SKIP: tokenId={token_id} expires in {diff_h}h')

    # Step 1: Reset vote state (required before withdraw if voted flag is set)
    if ve.functions.voted(token_id).call():
        try:
            tx = voter.functions.reset(token_id).build_transaction(_tx_params())
            try:
                tx['gas'] = _gas_limit(tx)
            except Exception:
                tx['gas'] = 300_000
            txh = _send(tx)
            log.info(f'[aero_vote_exit] reset tx={txh}')
            _sl.slog('reset', f'TX {txh[:10]}...', txhash=txh)
            time.sleep(4)
        except Exception as e:
            log.warning(f'[aero_vote_exit] reset failed (non-fatal): {e}')
    else:
        log.info(f'[aero_vote_exit] voted=False — skip reset')

    # Step 2: Withdraw veNFT -> AERO
    tx = ve.functions.withdraw(token_id).build_transaction(_tx_params())
    try:
        tx['gas'] = _gas_limit(tx)
    except Exception:
        tx['gas'] = 300_000
    txh = _send(tx)
    log.info(f'[aero_vote_exit] withdraw tx={txh}')
    _sl.slog('unlock', f'TX {txh[:10]}...', txhash=txh)
    time.sleep(4)

    # Step 3: AERO -> USDC (Aerodrome V1 router)
    aero_c   = w3.eth.contract(address=AERO_ADDR, abi=ERC20_ABI)
    aero_bal = aero_c.functions.balanceOf(WALLET).call()
    if aero_bal == 0:
        log.warning('[aero_vote_exit] No AERO after withdraw')
        _sl.slog('ok', f'tokenId={token_id} unlocked (no AERO to sell)')
        return txh

    log.info(f'[aero_vote_exit] Selling {aero_bal/1e18:.4f} AERO -> USDC')
    usdc_received = _aero_swap(AERO_ADDR, USDC_ADDR, aero_bal)
    if usdc_received == 0:
        log.warning('[aero_vote_exit] AERO->USDC returned 0 — skipping USDC->ETH')
        return txh

    # Step 4: USDC -> ETH (Uniswap V3)
    # Read actual on-chain balance — Aerodrome fee rounding may make it 1 wei
    # less than usdc_received, causing UniswapV3 STF revert on exactInputSingle.
    usdc_c      = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDR), abi=ERC20_ABI)
    usdc_actual = usdc_c.functions.balanceOf(WALLET).call()
    if usdc_actual < usdc_received:
        log.warning(f'[aero_vote_exit] USDC actual {usdc_actual} < expected {usdc_received} — using actual')
        usdc_received = usdc_actual
    if usdc_received == 0:
        log.warning('[aero_vote_exit] USDC balance 0 after AERO swap — skip ETH conversion')
        return txh
    log.info(f'[aero_vote_exit] Selling {usdc_received/1e6:.4f} USDC -> ETH')
    txh = swap_token_to_eth(USDC_ADDR, usdc_received)
    log.info(f'[aero_vote_exit] EXIT DONE  tokenId={token_id}  tx={txh}')
    _sl.slog('ok', f'tokenId={token_id} exited  TX {txh[:10]}...', txhash=txh)
    return txh
