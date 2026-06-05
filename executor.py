import os, time, logging
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

RPC_URL           = os.getenv('BASE_RPC_URL', 'https://mainnet.base.org')
DISCOVERY_RPC_URL = os.getenv('DISCOVERY_RPC_URL', RPC_URL)
PRIVATE_KEY = os.getenv('WALLET_PRIVATE_KEY')
WALLET      = Web3.to_checksum_address(os.getenv('WALLET_ADDRESS'))
MIN_ETH     = float(os.getenv('MIN_ETH_BALANCE', '0.005'))
USDC_ADDR   = Web3.to_checksum_address('0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')
USDS_ADDR   = Web3.to_checksum_address('0x820C137fa70C8691f0e44Dc420a5e53c168921Dc')
SUSDS_ADDR  = Web3.to_checksum_address('0x5875eEE11Cf8398102FdAd704C9E96607675467a')
PSM3_ADDR   = Web3.to_checksum_address('0x1601843c5E9bC251A3272907010AFa41Fa18347E')
DRY_RUN     = os.getenv('DRY_RUN', '').lower() in ('1', 'true', 'yes')
_DRY_GAS    = 300_000

w3      = Web3(Web3.HTTPProvider(RPC_URL))            # TX + complex calls
w3_read = Web3(Web3.HTTPProvider(DISCOVERY_RPC_URL))  # read-only queries (Alchemy)

# ── Minimal ABIs ───────────────────────────────────────────────────────────────

ERC20_ABI = [
    {"name": "approve",   "type": "function", "stateMutability": "nonpayable",
     "inputs":  [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs":  [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs":  [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]

COMET_ABI = [
    {"name": "supply",    "type": "function", "stateMutability": "nonpayable",
     "inputs":  [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": []},
    {"name": "withdraw",  "type": "function", "stateMutability": "nonpayable",
     "inputs":  [{"name": "asset", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": []},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs":  [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]

ERC4626_ABI = [
    {"name": "deposit",         "type": "function", "stateMutability": "nonpayable",
     "inputs":  [{"name": "assets", "type": "uint256"}, {"name": "receiver", "type": "address"}],
     "outputs": [{"name": "shares", "type": "uint256"}]},
    {"name": "withdraw",        "type": "function", "stateMutability": "nonpayable",
     "inputs":  [{"name": "assets", "type": "uint256"}, {"name": "receiver", "type": "address"}, {"name": "owner", "type": "address"}],
     "outputs": [{"name": "shares", "type": "uint256"}]},
    {"name": "balanceOf",       "type": "function", "stateMutability": "view",
     "inputs":  [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "convertToAssets", "type": "function", "stateMutability": "view",
     "inputs":  [{"name": "shares", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]

PSM3_ABI = [
    {"name": "swapExactIn", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "assetIn",       "type": "address"},
         {"name": "assetOut",      "type": "address"},
         {"name": "amountIn",      "type": "uint256"},
         {"name": "minAmountOut",  "type": "uint256"},
         {"name": "receiver",      "type": "address"},
         {"name": "referralCode",  "type": "uint256"},
     ],
     "outputs": [{"name": "amountOut", "type": "uint256"}]},
]

CTOKEN_ABI = [
    {"name": "mint",              "type": "function", "stateMutability": "nonpayable",
     "inputs":  [{"name": "mintAmount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "redeem",            "type": "function", "stateMutability": "nonpayable",
     "inputs":  [{"name": "redeemTokens", "type": "uint256"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "balanceOf",         "type": "function", "stateMutability": "view",
     "inputs":  [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "exchangeRateStored","type": "function", "stateMutability": "view",
     "inputs":  [],
     "outputs": [{"name": "", "type": "uint256"}]},
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def _rpc_call(fn, *args, retries: int = 3, delay: int = 5):
    """Call fn(*args) with retry on 429 rate-limit errors."""
    for _attempt in range(retries):
        try:
            return fn(*args)
        except Exception as _e:
            if '429' in str(_e) and _attempt < retries - 1:
                time.sleep(delay)
                continue
            raise

def _guard():
    bal = _rpc_call(w3.eth.get_balance, WALLET)
    if bal < Web3.to_wei(MIN_ETH, 'ether'):
        raise RuntimeError(f'ETH {Web3.from_wei(bal,"ether"):.5f} < MIN_ETH_BALANCE {MIN_ETH}')

_local_nonce: int | None = None

def _nonce() -> int:
    global _local_nonce
    if _local_nonce is None:
        _local_nonce = w3.eth.get_transaction_count(WALLET, 'pending')
    n = _local_nonce
    _local_nonce += 1
    return n

def reset_nonce() -> None:
    global _local_nonce
    _local_nonce = None

def _gas_price():
    return int(w3.eth.gas_price * 5)  # 5x buffer — Base fee cheap; extra margin prevents dropped TXs

def _tx_params(**extra) -> dict:
    """Base build_transaction params. gas=2_000_000 prevents web3 from calling estimate_gas
    inside build_transaction (which reverts on empty pools). Each function overwrites gas
    with _gas_limit() or a fallback immediately after build_transaction."""
    p = {'from': WALLET, 'nonce': _nonce(), 'gasPrice': _gas_price(), 'gas': 2_000_000, **extra}
    if DRY_RUN:
        p['gas'] = _DRY_GAS
    return p

def _gas_limit(tx: dict) -> int:
    if DRY_RUN:
        return _DRY_GAS
    return int(w3.eth.estimate_gas(tx) * 1.5)

def _send(tx: dict) -> str:
    if DRY_RUN:
        gas_price = tx.get('gasPrice', _gas_price())
        eth_cost  = float(Web3.from_wei(tx.get('gas', _DRY_GAS) * gas_price, 'ether'))
        log.info(
            f'[DRY RUN] SKIP TX  to={tx.get("to","?")}  '
            f'value={float(Web3.from_wei(tx.get("value", 0), "ether")):.6f} ETH  '
            f'gas={tx.get("gas", _DRY_GAS)}  gas_cost~{eth_cost:.6f} ETH'
        )
        return '0x' + 'dd' * 32
    global _local_nonce
    for _attempt in range(3):
        try:
            signed  = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            txh     = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=300)
            if receipt.status != 1:
                raise RuntimeError(f'TX reverted: {txh.hex()}')
            return txh.hex()
        except Exception as _e:
            _msg = str(_e)
            if 'nonce too low' in _msg and _attempt < 2:
                # Resync nonce from chain and patch the tx
                import time as _t
                _t.sleep(2)
                _local_nonce = w3.eth.get_transaction_count(WALLET, 'pending')
                new_nonce    = _local_nonce
                _local_nonce += 1
                tx = dict(tx)
                tx['nonce'] = new_nonce
                log.warning(f'nonce too low — resynced to {new_nonce}, retry {_attempt+1}/2')
                continue
            # TX timeout: dropped from sequencer mempool. Resync nonce to confirmed count
            # so the caller's retry doesn't queue behind the abandoned TX nonce.
            _type = type(_e).__name__
            if 'not found after' in _msg or 'TimeExhausted' in _type or 'timeout' in _msg.lower():
                import time as _t
                _t.sleep(2)
                _local_nonce = w3.eth.get_transaction_count(WALLET, 'latest')
                log.warning(f'TX timeout — nonce resynced to {_local_nonce} (latest confirmed)')
            raise

def _approve_if_needed(token_addr: str, spender: str, amount: int):
    token = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
    if token.functions.allowance(WALLET, spender).call() >= amount:
        return
    tx = token.functions.approve(spender, 2**256 - 1).build_transaction(
        _tx_params()
    )
    tx['gas'] = _gas_limit(tx)
    _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('approve', f'{_sym(token_addr)}')
    except Exception:
        pass
    # Wait for approval to propagate to RPC node before proceeding
    for _ in range(6):
        time.sleep(1)
        if token.functions.allowance(WALLET, spender).call() >= amount:
            return
    log.warning(f'Allowance not confirmed after 6s — RPC may be stale, proceeding anyway')

# ── Compound v3 (Comet) ────────────────────────────────────────────────────────

def compound_supply(comet_addr: str, token_addr: str, amount_wei: int) -> str:
    _guard()
    comet_addr = Web3.to_checksum_address(comet_addr)
    token_addr = Web3.to_checksum_address(token_addr)
    _approve_if_needed(token_addr, comet_addr, amount_wei)
    comet = w3.eth.contract(address=comet_addr, abi=COMET_ABI)
    tx = comet.functions.supply(token_addr, amount_wei).build_transaction(
        _tx_params()
    )
    tx['gas'] = _gas_limit(tx)
    txh = _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('supply', f'{_sym(token_addr)} → compound  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    return txh

def compound_withdraw(comet_addr: str, token_addr: str, amount_wei: int) -> str:
    _guard()
    comet_addr = Web3.to_checksum_address(comet_addr)
    token_addr = Web3.to_checksum_address(token_addr)
    comet = w3.eth.contract(address=comet_addr, abi=COMET_ABI)
    tx = comet.functions.withdraw(token_addr, amount_wei).build_transaction(
        _tx_params()
    )
    tx['gas'] = _gas_limit(tx)
    txh = _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('withdraw', f'{_sym(token_addr)} ← compound  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    return txh

# ── ERC-4626 (Fluid, Spark) ────────────────────────────────────────────────────

def erc4626_deposit(vault_addr: str, token_addr: str, amount_wei: int) -> str:
    _guard()
    vault_addr = Web3.to_checksum_address(vault_addr)
    token_addr = Web3.to_checksum_address(token_addr)
    _approve_if_needed(token_addr, vault_addr, amount_wei)
    vault = w3.eth.contract(address=vault_addr, abi=ERC4626_ABI)
    tx = vault.functions.deposit(amount_wei, WALLET).build_transaction(
        _tx_params()
    )
    try:
        tx['gas'] = _gas_limit(tx)
    except Exception:
        # Stale RPC may simulate with pre-swap balance → estimate_gas reverts.
        # Use safe fallback; MetaMorpho deposits typically consume 350k-500k gas.
        tx['gas'] = 800_000
        log.warning(f'estimate_gas failed for deposit {vault_addr} — using fallback gas=800000')
    txh = _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('supply', f'{_sym(token_addr)} → vault  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    return txh

def erc4626_withdraw_all(vault_addr: str) -> str:
    _guard()
    vault_addr = Web3.to_checksum_address(vault_addr)
    if DRY_RUN:
        log.info(f'[DRY RUN] SKIP erc4626_withdraw_all  vault={vault_addr}')
        return '0x' + 'dd' * 32
    vault  = w3.eth.contract(address=vault_addr, abi=ERC4626_ABI)
    shares = _rpc_call(vault.functions.balanceOf(WALLET).call)
    if shares == 0:
        raise RuntimeError(f'No shares in {vault_addr}')
    assets = _rpc_call(vault.functions.convertToAssets(shares).call)
    tx = vault.functions.withdraw(assets, WALLET, WALLET).build_transaction(
        _tx_params()
    )
    try:
        tx['gas'] = _gas_limit(tx)
    except Exception:
        # Some vaults (e.g. MetaMorpho) exceed public RPC simulation gas cap.
        # Use a safe high limit — actual consumption is usually 350k-500k.
        tx['gas'] = 800_000
        log.warning(f'estimate_gas failed for {vault_addr} — using fallback gas=800000')
    txh = _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('withdraw', f'vault  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    return txh

# ── Moonwell cToken ────────────────────────────────────────────────────────────

def ctoken_supply(ctoken_addr: str, token_addr: str, amount_wei: int) -> str:
    _guard()
    ctoken_addr = Web3.to_checksum_address(ctoken_addr)
    token_addr  = Web3.to_checksum_address(token_addr)
    _approve_if_needed(token_addr, ctoken_addr, amount_wei)
    ctoken = w3.eth.contract(address=ctoken_addr, abi=CTOKEN_ABI)
    tx = ctoken.functions.mint(amount_wei).build_transaction(
        _tx_params()
    )
    tx['gas'] = _gas_limit(tx)
    txh = _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('supply', f'{_sym(token_addr)} → mToken  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    return txh

def ctoken_withdraw_all(ctoken_addr: str) -> str:
    _guard()
    ctoken_addr = Web3.to_checksum_address(ctoken_addr)
    if DRY_RUN:
        log.info(f'[DRY RUN] SKIP ctoken_withdraw_all  ctoken={ctoken_addr}')
        return '0x' + 'dd' * 32
    ctoken = w3.eth.contract(address=ctoken_addr, abi=CTOKEN_ABI)
    shares = _rpc_call(ctoken.functions.balanceOf(WALLET).call)
    if shares == 0:
        raise RuntimeError(f'No cTokens in {ctoken_addr}')
    tx = ctoken.functions.redeem(shares).build_transaction(
        _tx_params()
    )
    tx['gas'] = _gas_limit(tx)
    txh = _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('withdraw', f'mToken  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    return txh

# ── Spark PSM3 (USDC ↔ USDS, 1:1 no fee) ─────────────────────────────────────

def psm_swap_usdc_to_usds(amount_usdc_wei: int) -> int:
    """Swap USDC → USDS via Spark PSM3. Returns USDS amount (wei, 18 dec)."""
    _guard()
    if DRY_RUN:
        usds_estimate = amount_usdc_wei * 10**12
        log.info(f'[DRY RUN] PSM USDC→USDS  in={amount_usdc_wei} USDC wei  out~={usds_estimate} USDS wei')
        return usds_estimate
    _approve_if_needed(USDC_ADDR, PSM3_ADDR, amount_usdc_wei)
    psm = w3.eth.contract(address=PSM3_ADDR, abi=PSM3_ABI)
    # 1:1 peg; allow 0.1% slippage. USDC 6dec → USDS 18dec (×1e12)
    min_usds = int(amount_usdc_wei * 10**12 * 999 // 1000)
    tx = psm.functions.swapExactIn(
        USDC_ADDR, USDS_ADDR, amount_usdc_wei, min_usds, WALLET, 0
    ).build_transaction(_tx_params())
    tx['gas'] = _gas_limit(tx)
    _send(tx)
    usds = w3.eth.contract(address=USDS_ADDR, abi=ERC20_ABI)
    return usds.functions.balanceOf(WALLET).call()


def psm_swap_usds_to_usdc(amount_usds_wei_hint: int) -> int:  # noqa
    """Swap all wallet USDS → USDC via Spark PSM3. Returns USDC amount (wei, 6 dec).
    In live mode queries actual balance to capture yield; hint used only for dry-run."""
    _guard()
    if DRY_RUN:
        usdc_estimate = amount_usds_wei_hint // 10**12
        log.info(f'[DRY RUN] PSM USDS→USDC  in~={amount_usds_wei_hint} USDS wei  out~={usdc_estimate} USDC wei')
        return usdc_estimate
    usds = w3.eth.contract(address=USDS_ADDR, abi=ERC20_ABI)
    actual = usds.functions.balanceOf(WALLET).call()
    if actual == 0:
        raise RuntimeError('psm_swap_usds_to_usdc: no USDS balance')
    _approve_if_needed(USDS_ADDR, PSM3_ADDR, actual)
    psm = w3.eth.contract(address=PSM3_ADDR, abi=PSM3_ABI)
    # USDS 18dec → USDC 6dec (÷1e12); allow 0.1% slippage
    min_usdc = int(actual // 10**12 * 999 // 1000)
    tx = psm.functions.swapExactIn(
        USDS_ADDR, USDC_ADDR, actual, min_usdc, WALLET, 0
    ).build_transaction(_tx_params())
    tx['gas'] = _gas_limit(tx)
    _send(tx)
    usdc = w3.eth.contract(address=USDC_ADDR, abi=ERC20_ABI)
    return usdc.functions.balanceOf(WALLET).call()


def psm_swap_usdc_to_susds(amount_usdc_wei: int) -> int:  # noqa
    """Swap USDC → sUSDS via Spark PSM3 (1 call). Returns sUSDS balance (wei, 18 dec)."""
    _guard()
    if DRY_RUN:
        susds_estimate = int(amount_usdc_wei * 10**12 * 0.91)
        log.info(f'[DRY RUN] PSM USDC->sUSDS  in={amount_usdc_wei} USDC wei  out~={susds_estimate} sUSDS wei')
        return susds_estimate
    _approve_if_needed(USDC_ADDR, PSM3_ADDR, amount_usdc_wei)
    psm = w3.eth.contract(address=PSM3_ADDR, abi=PSM3_ABI)
    min_susds = int(amount_usdc_wei * 10**12 * 90 // 100)  # allow 10% slippage (sUSDS > $1)
    tx = psm.functions.swapExactIn(
        USDC_ADDR, SUSDS_ADDR, amount_usdc_wei, min_susds, WALLET, 0
    ).build_transaction(_tx_params())
    tx['gas'] = _gas_limit(tx)
    txh = _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('psm_buy', f'USDC → sUSDS  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    susds = w3.eth.contract(address=SUSDS_ADDR, abi=ERC20_ABI)
    return susds.functions.balanceOf(WALLET).call()


def psm_swap_susds_to_usdc(amount_susds_wei_hint: int) -> int:
    """Swap all wallet sUSDS → USDC via Spark PSM3. Returns USDC amount (wei, 6 dec)."""
    _guard()
    if DRY_RUN:
        usdc_estimate = int(amount_susds_wei_hint // 10**12 * 110 // 100)
        log.info(f'[DRY RUN] PSM sUSDS->USDC  in~={amount_susds_wei_hint} sUSDS wei  out~={usdc_estimate} USDC wei')
        return usdc_estimate
    susds = w3.eth.contract(address=SUSDS_ADDR, abi=ERC20_ABI)
    actual = susds.functions.balanceOf(WALLET).call()
    if actual == 0:
        raise RuntimeError('psm_swap_susds_to_usdc: no sUSDS balance')
    _approve_if_needed(SUSDS_ADDR, PSM3_ADDR, actual)
    psm = w3.eth.contract(address=PSM3_ADDR, abi=PSM3_ABI)
    min_usdc = int(actual // 10**12 * 90 // 100)  # 10% slippage floor
    tx = psm.functions.swapExactIn(
        SUSDS_ADDR, USDC_ADDR, actual, min_usdc, WALLET, 0
    ).build_transaction(_tx_params())
    tx['gas'] = _gas_limit(tx)
    txh = _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('psm_sell', f'sUSDS → USDC  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    usdc = w3.eth.contract(address=USDC_ADDR, abi=ERC20_ABI)
    return usdc.functions.balanceOf(WALLET).call()


# ── Beefy Vault ────────────────────────────────────────────────────────────────

BEEFY_ABI = [
    {"name": "deposit",     "type": "function", "stateMutability": "nonpayable",
     "inputs":  [{"name": "_amount", "type": "uint256"}], "outputs": []},
    {"name": "withdrawAll", "type": "function", "stateMutability": "nonpayable",
     "inputs":  [], "outputs": []},
    {"name": "balanceOf",   "type": "function", "stateMutability": "view",
     "inputs":  [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "want",        "type": "function", "stateMutability": "view",
     "inputs":  [], "outputs": [{"name": "", "type": "address"}]},
]

GAUGE_ABI = [
    {"name": "deposit",   "type": "function", "stateMutability": "nonpayable",
     "inputs":  [{"name": "_amount", "type": "uint256"}], "outputs": []},
    {"name": "withdraw",  "type": "function", "stateMutability": "nonpayable",
     "inputs":  [{"name": "_amount", "type": "uint256"}], "outputs": []},
    {"name": "getReward", "type": "function", "stateMutability": "nonpayable",
     "inputs":  [{"name": "_account", "type": "address"}], "outputs": []},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs":  [{"name": "_account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]

AERODROME_ROUTER_ABI = [
    {"name": "addLiquidity", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "tokenA",          "type": "address"},
         {"name": "tokenB",          "type": "address"},
         {"name": "stable",          "type": "bool"},
         {"name": "amountADesired",  "type": "uint256"},
         {"name": "amountBDesired",  "type": "uint256"},
         {"name": "amountAMin",      "type": "uint256"},
         {"name": "amountBMin",      "type": "uint256"},
         {"name": "to",              "type": "address"},
         {"name": "deadline",        "type": "uint256"},
     ],
     "outputs": [
         {"name": "amountA",   "type": "uint256"},
         {"name": "amountB",   "type": "uint256"},
         {"name": "liquidity", "type": "uint256"},
     ]},
    {"name": "removeLiquidity", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "tokenA",     "type": "address"},
         {"name": "tokenB",     "type": "address"},
         {"name": "stable",     "type": "bool"},
         {"name": "liquidity",  "type": "uint256"},
         {"name": "amountAMin", "type": "uint256"},
         {"name": "amountBMin", "type": "uint256"},
         {"name": "to",         "type": "address"},
         {"name": "deadline",   "type": "uint256"},
     ],
     "outputs": [
         {"name": "amountA", "type": "uint256"},
         {"name": "amountB", "type": "uint256"},
     ]},
]

AERODROME_ROUTER   = Web3.to_checksum_address('0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43')
AERODROME_FACTORY  = Web3.to_checksum_address('0x420DD381b31aEf6683db6B902084cB0FFECe40Da')

AERODROME_ROUTER_SWAP_ABI = [
    {"name": "swapExactTokensForTokens", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "amountIn",    "type": "uint256"},
         {"name": "amountOutMin","type": "uint256"},
         {"name": "routes",      "type": "tuple[]",
          "components": [
              {"name": "from",    "type": "address"},
              {"name": "to",      "type": "address"},
              {"name": "stable",  "type": "bool"},
              {"name": "factory", "type": "address"},
          ]},
         {"name": "to",       "type": "address"},
         {"name": "deadline", "type": "uint256"},
     ],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
]


def beefy_deposit(vault_addr: str, token_addr: str, amount_wei: int) -> str:
    """Deposit `amount_wei` of `token_addr` into Beefy vault. Returns tx hash."""
    _guard()
    vault_addr = Web3.to_checksum_address(vault_addr)
    token_addr = Web3.to_checksum_address(token_addr)
    _approve_if_needed(token_addr, vault_addr, amount_wei)
    vault = w3.eth.contract(address=vault_addr, abi=BEEFY_ABI)
    tx = vault.functions.deposit(amount_wei).build_transaction(_tx_params())
    try:
        tx['gas'] = _gas_limit(tx)
    except Exception:
        tx['gas'] = 2_000_000
        log.warning(f'estimate_gas failed for beefy_deposit {vault_addr} — fallback gas=2000000')
    txh = _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('supply', f'beefy vault  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    return txh


def beefy_withdraw_all(vault_addr: str) -> str:
    """Withdraw all mooToken shares from Beefy vault. Returns tx hash."""
    _guard()
    vault_addr = Web3.to_checksum_address(vault_addr)
    if DRY_RUN:
        log.info(f'[DRY RUN] SKIP beefy_withdraw_all  vault={vault_addr}')
        return '0x' + 'dd' * 32
    vault  = w3.eth.contract(address=vault_addr, abi=BEEFY_ABI)
    shares = _rpc_call(vault.functions.balanceOf(WALLET).call)
    if shares == 0:
        raise RuntimeError(f'No mooToken shares in {vault_addr}')
    tx = vault.functions.withdrawAll().build_transaction(_tx_params())
    try:
        tx['gas'] = _gas_limit(tx)
    except Exception:
        tx['gas'] = 600_000
        log.warning(f'estimate_gas failed for beefy_withdraw_all — fallback gas=600000')
    txh = _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('withdraw', f'beefy vault  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    return txh


def aerodrome_add_liquidity(
    token0: str, token1: str, stable: bool,
    amount0_wei: int, amount1_wei: int,
    slippage: float = 0.05,
) -> tuple:
    """Add liquidity to Aerodrome v1 pool. Returns (txh, lp_wei_received)."""
    _guard()
    token0 = Web3.to_checksum_address(token0)
    token1 = Web3.to_checksum_address(token1)
    lp_contract = w3.eth.contract(
        address=_aerodrome_pool_addr(token0, token1, stable), abi=ERC20_ABI
    )
    lp_before = lp_contract.functions.balanceOf(WALLET).call()

    # min=0: router picks optimal ratio — leftover tokens returned to wallet
    min0 = 0
    min1 = 0
    deadline = w3.eth.get_block('latest')['timestamp'] + 600

    _approve_if_needed(token0, AERODROME_ROUTER, amount0_wei)
    _approve_if_needed(token1, AERODROME_ROUTER, amount1_wei)

    router = w3.eth.contract(address=AERODROME_ROUTER, abi=AERODROME_ROUTER_ABI)
    tx = router.functions.addLiquidity(
        token0, token1, stable,
        amount0_wei, amount1_wei,
        min0, min1,
        WALLET, deadline,
    ).build_transaction(_tx_params())
    try:
        tx['gas'] = _gas_limit(tx)
    except Exception:
        tx['gas'] = 600_000
        log.warning('estimate_gas failed for aerodrome_add_liquidity — fallback gas=600000')
    txh = _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('add_lp', f'{_sym(token0)}/{_sym(token1)}  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass

    time.sleep(4)
    lp_after = lp_contract.functions.balanceOf(WALLET).call()
    lp_received = max(lp_after - lp_before, 0)
    log.info(f'aerodrome_add_liquidity: LP received={lp_received}')
    return txh, lp_received


def aerodrome_swap_stable(token_in: str, token_out: str, amount_in_wei: int, min_out_wei: int = 0) -> str:
    """Swap token_in -> token_out via Aerodrome sAMM stable route. Returns tx hash."""
    _guard()
    token_in  = Web3.to_checksum_address(token_in)
    token_out = Web3.to_checksum_address(token_out)
    deadline  = w3.eth.get_block('latest')['timestamp'] + 600
    _approve_if_needed(token_in, AERODROME_ROUTER, amount_in_wei)
    router = w3.eth.contract(address=AERODROME_ROUTER, abi=AERODROME_ROUTER_SWAP_ABI)
    routes = [{'from': token_in, 'to': token_out, 'stable': True, 'factory': AERODROME_FACTORY}]
    tx = router.functions.swapExactTokensForTokens(
        amount_in_wei, min_out_wei, routes, WALLET, deadline,
    ).build_transaction(_tx_params())
    try:
        tx['gas'] = _gas_limit(tx)
    except Exception:
        tx['gas'] = 300_000
        log.warning(f'estimate_gas failed for aerodrome_swap_stable — fallback gas=300000')
    txh = _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('swap_stable', f'{_sym(token_in)} → {_sym(token_out)}  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    log.info(f'aerodrome_swap_stable: {token_in[:8]}...->{token_out[:8]}... in={amount_in_wei}')
    return txh


def aerodrome_remove_liquidity(
    token0: str, token1: str, stable: bool,
    lp_amount: int,
    slippage: float = 0.05,
) -> str:
    """Remove liquidity from Aerodrome v1 pool. Returns tx hash."""
    _guard()
    token0 = Web3.to_checksum_address(token0)
    token1 = Web3.to_checksum_address(token1)
    pool_addr = _aerodrome_pool_addr(token0, token1, stable)
    deadline  = w3.eth.get_block('latest')['timestamp'] + 600

    _approve_if_needed(pool_addr, AERODROME_ROUTER, lp_amount)

    router = w3.eth.contract(address=AERODROME_ROUTER, abi=AERODROME_ROUTER_ABI)
    tx = router.functions.removeLiquidity(
        token0, token1, stable,
        lp_amount, 0, 0,
        WALLET, deadline,
    ).build_transaction(_tx_params())
    tx['gas'] = _gas_limit(tx)
    txh = _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('remove_lp', f'{_sym(token0)}/{_sym(token1)}  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    log.info(f'aerodrome_remove_liquidity: LP={lp_amount} removed')
    return txh


def aerodrome_gauge_stake(pool_addr: str, gauge_addr: str, lp_amount: int) -> str:
    """Approve gauge to spend LP tokens, then stake. Returns tx hash."""
    _guard()
    pool_addr  = Web3.to_checksum_address(pool_addr)
    gauge_addr = Web3.to_checksum_address(gauge_addr)
    _approve_if_needed(pool_addr, gauge_addr, lp_amount)
    gauge = w3.eth.contract(address=gauge_addr, abi=GAUGE_ABI)
    tx = gauge.functions.deposit(lp_amount).build_transaction(_tx_params())
    try:
        tx['gas'] = _gas_limit(tx)
    except Exception:
        tx['gas'] = 400_000
        log.warning(f'estimate_gas failed for gauge_stake {gauge_addr} — fallback gas=400000')
    txh = _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('stake', f'gauge  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    return txh


def aerodrome_gauge_unstake(gauge_addr: str, lp_amount: int) -> str:
    """Unstake LP tokens from Aerodrome gauge. Returns tx hash."""
    _guard()
    gauge_addr = Web3.to_checksum_address(gauge_addr)
    gauge = w3.eth.contract(address=gauge_addr, abi=GAUGE_ABI)
    tx = gauge.functions.withdraw(lp_amount).build_transaction(_tx_params())
    try:
        tx['gas'] = _gas_limit(tx)
    except Exception:
        tx['gas'] = 300_000
        log.warning(f'estimate_gas failed for gauge_unstake {gauge_addr} — fallback gas=300000')
    txh = _send(tx)
    try:
        import step_logger as _sl
        _sl.slog('unstake', f'gauge  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    return txh


def aerodrome_gauge_claim(gauge_addr: str) -> str:
    """Claim AERO rewards from gauge. Returns tx hash."""
    _guard()
    gauge_addr = Web3.to_checksum_address(gauge_addr)
    gauge = w3.eth.contract(address=gauge_addr, abi=GAUGE_ABI)
    tx = gauge.functions.getReward(WALLET).build_transaction(_tx_params())
    try:
        tx['gas'] = _gas_limit(tx)
    except Exception:
        tx['gas'] = 200_000
        log.warning(f'estimate_gas failed for gauge_claim {gauge_addr} — fallback gas=200000')
    return _send(tx)


def _aerodrome_pool_addr(token0: str, token1: str, stable: bool) -> str:
    """Return checksum pool address for a given token pair from config."""
    import json, os
    with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as f:
        cfg = json.load(f)
    for p in cfg['platforms'].values():
        if p.get('type') not in ('beefy_lp', 'aero_lp'):
            continue
        t0 = Web3.to_checksum_address(p.get('token0_address', ''))
        t1 = Web3.to_checksum_address(p.get('token1_address', ''))
        lp = p.get('lp_address') or p.get('pool_address', '')
        if {t0, t1} == {token0, token1} and p.get('stable') == stable:
            return Web3.to_checksum_address(lp)
    raise ValueError(f'No LP pool found for {token0}/{token1} stable={stable}')


# ── Balance queries ────────────────────────────────────────────────────────────

def get_eth_balance() -> float:
    return float(Web3.from_wei(_rpc_call(w3_read.eth.get_balance, WALLET), 'ether'))

def get_token_balance(token_addr: str, decimals: int = 6) -> float:
    token = w3_read.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
    raw   = _rpc_call(token.functions.balanceOf(WALLET).call)
    return raw / 10**decimals


# ── Price oracles ──────────────────────────────────────────────────────────────

_CHAINLINK_PRICE_ABI = [
    {"name": "latestRoundData", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [
         {"name": "roundId",         "type": "uint80"},
         {"name": "answer",          "type": "int256"},
         {"name": "startedAt",       "type": "uint256"},
         {"name": "updatedAt",       "type": "uint256"},
         {"name": "answeredInRound", "type": "uint80"},
     ]},
]

POOL_RESERVES_ABI = [
    {"name": "getReserves", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [
         {"name": "_reserve0",        "type": "uint256"},
         {"name": "_reserve1",        "type": "uint256"},
         {"name": "_blockTimestampLast", "type": "uint32"},
     ]},
    {"name": "token0", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
]

_V3_SLOT0_ABI = [
    {"name": "slot0", "type": "function", "stateMutability": "view",
     "inputs": [],
     "outputs": [
         {"name": "sqrtPriceX96",              "type": "uint160"},
         {"name": "tick",                       "type": "int24"},
         {"name": "observationIndex",           "type": "uint16"},
         {"name": "observationCardinality",     "type": "uint16"},
         {"name": "observationCardinalityNext", "type": "uint16"},
         {"name": "feeProtocol",                "type": "uint32"},
         {"name": "unlocked",                   "type": "bool"},
     ]},
]

_ETH_USD_FEED    = '0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70'
_CFG_CACHE       = None
_PRICE_CACHE     = {}   # symbol -> (price, timestamp)
_PRICE_CACHE_TTL = 120  # seconds — reuse prices within a multi-pool run
_ADDR_SYM: dict  = {}   # addr_lower -> symbol cache for step logging


def _sym(addr: str) -> str:
    """Return token symbol for address, or short hex fallback."""
    a = addr.lower()
    if not _ADDR_SYM:
        try:
            cfg = _load_cfg()
            for s, t in cfg.get('tokens', {}).items():
                _ADDR_SYM[t['address'].lower()] = s
        except Exception:
            pass
    return _ADDR_SYM.get(a, addr[:8] + '...')


def _load_cfg() -> dict:
    global _CFG_CACHE
    if _CFG_CACHE is None:
        import json
        with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as f:
            _CFG_CACHE = json.load(f)
    return _CFG_CACHE


def get_eth_usd_price() -> float:
    feed = w3_read.eth.contract(
        address=Web3.to_checksum_address(_ETH_USD_FEED), abi=_CHAINLINK_PRICE_ABI
    )
    _, answer, _, _, _ = feed.functions.latestRoundData().call()
    return answer / 1e8


def get_token_usd_price(symbol: str) -> float:
    """Return USD price for symbol. Cached 120s to avoid 429 during multi-pool runs."""
    import time as _time
    cached = _PRICE_CACHE.get(symbol)
    if cached and _time.time() - cached[1] < _PRICE_CACHE_TTL:
        return cached[0]

    if symbol in ('USDC', 'USDS', 'DOLA', 'USDz', 'sUSDS', 'USDT'):
        price = 1.0
    elif symbol == 'WETH':
        price = get_eth_usd_price()
    else:
        eth_usd = get_token_usd_price('WETH')
        cfg = _load_cfg()
        tok = cfg['tokens'].get(symbol, {})
        feed_addr = tok.get('chainlink_feed')
        if feed_addr:
            feed_c = w3_read.eth.contract(
                address=Web3.to_checksum_address(feed_addr), abi=_CHAINLINK_PRICE_ABI
            )
            _, answer, _, _, _ = feed_c.functions.latestRoundData().call()
            rate = answer / 10**tok.get('feed_decimals', 8)
            price = rate * eth_usd if tok.get('feed_type') == 'eth_rate' else rate
        elif symbol == 'AERO':
            pool_addr = cfg['platforms'].get('aero_lp_usdc_aero', {}).get('pool_address')
            pool_c = w3_read.eth.contract(
                address=Web3.to_checksum_address(pool_addr), abi=POOL_RESERVES_ABI
            )
            r0, r1, _ = pool_c.functions.getReserves().call()
            price = (r0 / 1e6) / (r1 / 1e18) if r1 > 0 else 1.0
        elif symbol == 'VIRTUAL':
            pool_addr = cfg['platforms'].get('aero_lp_virtual_weth', {}).get('pool_address')
            pool_c = w3_read.eth.contract(
                address=Web3.to_checksum_address(pool_addr), abi=POOL_RESERVES_ABI
            )
            r0, r1, _ = pool_c.functions.getReserves().call()
            price = (r1 / 1e18) / (r0 / 1e18) * eth_usd if r0 > 0 else 0.5
        elif symbol == 'CAKE':
            # CAKE/WETH PancakeSwap v3 pool — CAKE=token0 (0x30<0x42), WETH=token1, both 18 dec
            # sqrtPriceX96: sqrt(token1/token0) in Q96 → price = (sqrt/2^96)^2 = WETH per CAKE
            pool_addr = cfg['dex'].get('cake_weth_v3_pool')
            pool_c = w3_read.eth.contract(
                address=Web3.to_checksum_address(pool_addr), abi=_V3_SLOT0_ABI
            )
            sqrt_price_x96 = pool_c.functions.slot0().call()[0]
            weth_per_cake = (sqrt_price_x96 / 2**96) ** 2
            price = eth_usd * weth_per_cake
        elif symbol == 'MORPHO':
            # Uni3 USDC/MORPHO pool fee=10000 — USDC=token0(6dec), MORPHO=token1(18dec)
            # ratio = (sqrt/2^96)^2 = morpho_raw/usdc_raw
            # morpho_per_usdc = ratio/1e12 → usd_per_morpho = 1e12/ratio
            pool_addr = cfg['dex'].get('morpho_usdc_uni_pool')
            pool_c = w3_read.eth.contract(
                address=Web3.to_checksum_address(pool_addr), abi=_V3_SLOT0_ABI
            )
            sqrt_price_x96 = pool_c.functions.slot0().call()[0]
            ratio = (sqrt_price_x96 / 2**96) ** 2
            price = 1e12 / ratio if ratio > 0 else 2.0
        elif symbol == 'cbXRP':
            # Cake3 WETH/cbXRP pool fee=500 — WETH=token0(18dec), cbXRP=token1(6dec)
            # PancakeSwap v3 has negative tick → decode slot0 via raw eth_call (avoids ABI issue)
            # ratio = (sqrt/2^96)^2 = cbxrp_raw/weth_raw
            # cbxrp_per_weth = ratio*1e12 → usd_per_cbxrp = eth_usd/(ratio*1e12)
            pool_addr = cfg['dex'].get('cbxrp_weth_cake_pool')
            slot0_sel = w3_read.keccak(text='slot0()')[:4]
            raw = w3_read.eth.call({'to': Web3.to_checksum_address(pool_addr), 'data': slot0_sel})
            sqrt_price_x96 = int.from_bytes(raw[:32], 'big')
            ratio = (sqrt_price_x96 / 2**96) ** 2
            price = eth_usd / (ratio * 1e12) if ratio > 0 else 1.0
        elif symbol == 'weETH':
            # Uni3 weETH/WETH fee=100 — weETH=token0(0x04<0x42), WETH=token1, both 18dec
            # ratio = (sqrt/2^96)^2 = WETH per weETH (weETH accrues ETH staking rewards, ratio > 1)
            pool_addr = cfg['dex'].get('weeth_weth_v3_pool')
            pool_c = w3_read.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=_V3_SLOT0_ABI)
            sqrt_price_x96 = pool_c.functions.slot0().call()[0]
            weth_per_weeth = (sqrt_price_x96 / 2**96) ** 2
            price = eth_usd * weth_per_weeth
        else:
            raise ValueError(f'No price source for {symbol}')

    _PRICE_CACHE[symbol] = (price, _time.time())
    return price


def get_aero_lp_deposit_amounts(
    p: dict, tokens_cfg: dict, budget_usd: float = 10.0
) -> tuple:
    """
    Return (t0_addr, t1_addr, stable, amt0_wei, amt1_wei) for an aero_lp pool.
    Splits budget proportionally to current pool reserves so both tokens
    are fully used with minimal residuals.
    """
    t0_sym  = p['token0']
    t1_sym  = p['token1']
    t0_addr = Web3.to_checksum_address(p['token0_address'])
    t1_addr = Web3.to_checksum_address(p['token1_address'])
    stable  = p.get('stable', False)

    t0_dec = tokens_cfg.get(t0_sym, {}).get('decimals', 18)
    t1_dec = tokens_cfg.get(t1_sym, {}).get('decimals', 18)

    pool_c = w3.eth.contract(
        address=Web3.to_checksum_address(p['pool_address']), abi=POOL_RESERVES_ABI
    )
    r0, r1, _ = pool_c.functions.getReserves().call()

    # AMM sorts tokens by address — verify actual pool token0 matches config order
    actual_t0 = pool_c.functions.token0().call()
    if actual_t0.lower() != t0_addr.lower():
        # Pool order is reversed vs config — swap reserves to align with config
        r0, r1 = r1, r0

    p0 = get_token_usd_price(t0_sym)
    p1 = get_token_usd_price(t1_sym)

    r0_usd = (r0 / 10**t0_dec) * p0
    r1_usd = (r1 / 10**t1_dec) * p1
    total  = r0_usd + r1_usd or 1.0

    ratio0   = r0_usd / total
    amt0_usd = budget_usd * ratio0
    amt1_usd = budget_usd * (1.0 - ratio0)

    amt0_wei = int(amt0_usd / p0 * 10**t0_dec) if p0 > 0 else 0
    amt1_wei = int(amt1_usd / p1 * 10**t1_dec) if p1 > 0 else 0

    log.info(
        f'aero_lp ratio {t0_sym}:{t1_sym} = {ratio0*100:.1f}:{(1-ratio0)*100:.1f} '
        f'| ${amt0_usd:.2f} {t0_sym} + ${amt1_usd:.2f} {t1_sym}'
    )
    return t0_addr, t1_addr, stable, amt0_wei, amt1_wei
