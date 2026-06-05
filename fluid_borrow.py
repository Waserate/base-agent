"""
fluid_borrow.py — Fluid T1 vault borrow module for Base chain.

Each Fluid T1 vault supports one collateral/debt pair via a single operate() call.
Positions are represented as ERC721 NFTs minted by the vault factory.

Supported vaults (7 platforms, 2 tiers):
  Tier 1 — cbBTC borrow (low utilization ~9%):
    fl_eth_cbbtc    ETH  → cbBTC   CF=86%  LT=90%
    fl_wsteth_cbbtc wstETH → cbBTC  CF=85%  LT=88%
  Tier 1 — correlated pair loops (very safe):
    fl_wsteth_eth   wstETH → ETH   CF=93%  LT=95%
    fl_cbbtc_eth    cbBTC  → ETH   CF=86%  LT=90%
  Tier 2 — USDC borrow (high pool utilization ~97%):
    fl_eth_usdc     ETH  → USDC   CF=85%  LT=88%
    fl_wsteth_usdc  wstETH → USDC  CF=80%  LT=85%
    fl_cbbtc_usdc   cbBTC  → USDC  CF=80%  LT=85%

Vault factory (Base): 0x324c5Dc1fC42c7a4D43d92df1eBA58a54d13Bf2d

State encoding (stored in positions.amount_wei as string):
  "nftId:{id}||COL:{sym}:{wei}||BOR:{sym}:{wei}"
  e.g. "nftId:1234||COL:wstETH:2000000000000000||BOR:cbBTC:6800"

Platform config keys (contracts.json):
  vault_address          : Fluid T1 vault proxy address
  collateral_token       : symbol  ("ETH" for native, "wstETH", "cbBTC")
  collateral_address     : ERC20 addr (or ETH_SENTINEL for native ETH)
  collateral_decimals    : int
  collateral_amount_wei  : how much collateral to deposit (in wei)
  collateral_cf          : float  (e.g. 0.86 = 86% collateral factor)
  borrow_token           : symbol  ("ETH", "cbBTC", "USDC")
  borrow_address         : ERC20 addr (or ETH_SENTINEL for native ETH)
  borrow_decimals        : int
  ltv_min / ltv_max      : float  — random borrow LTV each open
  expiry_days            : [min, max]
"""

import os, time, random, logging
from web3 import Web3
from dotenv import load_dotenv
import executor
import swap as _swap
from swap import PriceGuardError, ConfigError, SwapExecutionError

load_dotenv()
log = logging.getLogger(__name__)

DRY_RUN        = os.getenv('DRY_RUN', '').lower() in ('1', 'true', 'yes')
REPAY_BUFFER   = 1.03    # 3% extra for ETH value sent — actual debt may be slightly higher
HEALTH_CLOSE_THRESHOLD = 1.5
INT256_MIN = -(2**255)   # Fluid sentinel: "repay/withdraw all" (full close)

# Native ETH sentinel address (Fluid uses this instead of WETH for native ETH vaults)
ETH_SENTINEL = '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE'

# ERC721 Transfer event topic (used to parse minted NFT ID from operate() receipt)
TRANSFER_TOPIC = Web3.keccak(text='Transfer(address,address,uint256)').hex()

# Fluid T1 vault operate() ABI
# Returns (nftId, actualCol, actualDebt) but we read nftId from Transfer event instead.
_VAULT_ABI = [
    {
        'name': 'operate',
        'type': 'function',
        'stateMutability': 'payable',
        'inputs': [
            {'name': 'nftId_',   'type': 'uint256'},
            {'name': 'newCol_',  'type': 'int256'},
            {'name': 'newDebt_', 'type': 'int256'},
            {'name': 'to_',      'type': 'address'},
        ],
        'outputs': [
            {'name': 'nftId__', 'type': 'uint256'},
            {'name': 'col_',    'type': 'int256'},
            {'name': 'debt_',   'type': 'int256'},
        ],
    },
]


def _vault(addr: str):
    return executor.w3.eth.contract(
        address=Web3.to_checksum_address(addr), abi=_VAULT_ABI
    )


def _is_eth(token_address: str) -> bool:
    return token_address.lower() == ETH_SENTINEL.lower()


# ── State encoding ────────────────────────────────────────────────────────────

def encode_state(nft_id: int, col_sym: str, col_wei: int,
                 bor_sym: str, bor_wei: int) -> str:
    return f'nftId:{nft_id}||COL:{col_sym}:{col_wei}||BOR:{bor_sym}:{bor_wei}'


def parse_state(encoded: str) -> dict:
    """Returns {'nft_id', 'col_sym', 'col_wei', 'bor_sym', 'bor_wei'}."""
    parts = encoded.split('||')
    nft_id  = int(parts[0].split(':')[1])
    _, col_sym, col_wei = parts[1].split(':')
    _, bor_sym, bor_wei = parts[2].split(':')
    return {
        'nft_id':  nft_id,
        'col_sym': col_sym,  'col_wei': int(col_wei),
        'bor_sym': bor_sym,  'bor_wei': int(bor_wei),
    }


# ── NFT ID extraction from operate() receipt ─────────────────────────────────

def _extract_nft_from_receipt(txh: str) -> int:
    """Parse ERC721 Transfer(0x0 → wallet) from operate() receipt to get minted nftId.

    Topic format: HexBytes.hex() has NO 0x prefix; Web3.keccak().hex() HAS 0x prefix.
    Normalise by stripping 0x from everything before comparing.
    """
    try:
        receipt = executor.w3.eth.get_transaction_receipt(txh)
        # Normalise: strip ONLY '0x' prefix (not leading zeros — zero address would vanish)
        def bare(t):
            h = (t.hex() if isinstance(t, bytes) else str(t)).lower()
            return h[2:] if h.startswith('0x') else h

        sig_bare    = bare(TRANSFER_TOPIC)          # 'ddf252...' (64 chars)
        wallet_bare = executor.WALLET.lower()[2:].zfill(64)  # 64 hex chars
        zero_bare   = '0' * 64                      # 64 zeros for address(0)

        for log_entry in receipt.logs:
            raw_topics = log_entry['topics']
            if len(raw_topics) != 4:
                continue
            t0 = bare(raw_topics[0])
            t1 = bare(raw_topics[1]).zfill(64)   # pad in case HexBytes drop leading 0s
            t2 = bare(raw_topics[2]).zfill(64)
            t3 = bare(raw_topics[3]).zfill(64)

            if (t0 == sig_bare
                    and t1 == zero_bare
                    and t2 == wallet_bare):
                nft_id = int(t3, 16)
                log.info(f'fluid_borrow: NFT minted tokenId={nft_id}')
                return nft_id

        # Debug: show all 4-topic logs if NFT not found
        log.warning('fluid_borrow: NFT Transfer not found. All 4-topic logs:')
        for le in receipt.logs:
            if len(le['topics']) == 4:
                log.warning(f'  addr={le["address"]}  topic0={le["topics"][0].hex()[:12]}...'
                            f'  t1={le["topics"][1].hex()[-12:]}  t2={le["topics"][2].hex()[-12:]}')
    except Exception as e:
        log.warning(f'fluid_borrow: could not extract NFT from receipt: {e}')
    return 0


# ── Acquire / release helpers ─────────────────────────────────────────────────

def _acquire_col(sym: str, addr: str, amount_wei: int):
    """Acquire collateral token. ETH = already in wallet (no-op). ERC20 = swap ETH→token."""
    if DRY_RUN:
        log.info(f'[DRY RUN] acquire collateral {sym} {amount_wei}')
        return
    if _is_eth(addr):
        return   # native ETH already in wallet — just guard in open_borrow
    if sym == 'WETH':
        _swap.wrap_eth(amount_wei)
        time.sleep(2)
    else:
        try:
            _swap.attempt_swap(_swap.swap_eth_to_token,
                               Web3.to_checksum_address(addr), amount_wei)
        except (PriceGuardError, ConfigError, SwapExecutionError) as e:
            raise RuntimeError(f'fluid_borrow acquire {sym}: {e}')
        time.sleep(2)


def _release_to_eth(sym: str, addr: str, amount_wei: int):
    """Convert token → ETH. Best-effort (non-fatal)."""
    if DRY_RUN:
        log.info(f'[DRY RUN] release {sym} → ETH')
        return
    if _is_eth(addr):
        return   # already ETH
    try:
        if sym == 'WETH':
            _swap.unwrap_all_weth()
        else:
            bal = executor.w3.eth.contract(
                address=Web3.to_checksum_address(addr), abi=executor.ERC20_ABI
            ).functions.balanceOf(executor.WALLET).call()
            if bal > 0:
                _swap.attempt_swap(_swap.swap_token_to_eth,
                                   Web3.to_checksum_address(addr), bal)
    except Exception as e:
        log.warning(f'fluid_borrow release {sym}→ETH failed (non-fatal): {e}')


# ── Health check ──────────────────────────────────────────────────────────────

def check_health(encoded: str, p: dict) -> float:
    """
    Estimate health factor from stored state (no on-chain query needed).
    health = (col_wei/dec × price × CF) / (bor_wei_with_interest/dec × bor_price)
    Approximate: assumes interest accrued up to 3%/year for 10 days = 0.08% extra.
    Returns 999.0 in DRY_RUN.
    """
    if DRY_RUN:
        return 999.0
    try:
        s    = parse_state(encoded)
        col_dec = int(p['collateral_decimals'])
        bor_dec = int(p['borrow_decimals'])
        cf      = float(p['collateral_cf'])

        col_sym = p['collateral_token']
        bor_sym = p['borrow_token']

        _ps = lambda s: 'WETH' if s == 'ETH' else s
        col_price = 1.0 if col_sym in ('USDC', 'USDS') else executor.get_token_usd_price(_ps(col_sym))
        bor_price = 1.0 if bor_sym in ('USDC', 'USDS') else executor.get_token_usd_price(_ps(bor_sym))

        # Estimate debt with accrued interest (assume worst-case 5% APY, 14 days)
        interest_factor = 1.0 + (0.05 * 14 / 365)
        col_usd = (s['col_wei'] / 10**col_dec) * col_price * cf
        bor_usd = (s['bor_wei'] / 10**bor_dec) * bor_price * interest_factor

        return col_usd / bor_usd if bor_usd > 0 else 999.0
    except Exception as e:
        log.warning(f'fluid_borrow check_health error: {e}')
        return 999.0


# ── Open borrow ───────────────────────────────────────────────────────────────

def open_borrow(p: dict, collateral_usd: float = 0.0) -> tuple:
    """
    Open a Fluid T1 borrow position.

    Returns (encoded_state: str, borrow_txh: str)

    Flow:
      1. Acquire collateral (ETH = wallet already has; ERC20 = swap from ETH)
      2. If ERC20 collateral: approve vault
      3. Call vault.operate(0, +col_wei, +borrow_wei, wallet)
         - If ETH collateral: send value=col_wei
      4. Extract minted NFT ID from Transfer event in receipt
      5. Convert borrowed token → ETH immediately (unless already ETH)

    collateral_usd: if >0, override config collateral_amount_wei from live price.
    """
    executor._guard()

    vault_addr  = p['vault_address']
    col_sym     = p['collateral_token']
    col_addr    = p['collateral_address']
    col_dec     = int(p['collateral_decimals'])
    bor_sym     = p['borrow_token']
    bor_addr    = p['borrow_address']
    bor_dec     = int(p['borrow_decimals'])
    ltv_min     = float(p.get('ltv_min', 0.10))
    ltv_max     = float(p.get('ltv_max', 0.20))

    ltv = random.uniform(ltv_min, ltv_max)

    # Price-based borrow amount
    # 'ETH' native maps to 'WETH' for price lookup
    _price_sym = lambda s: 'WETH' if s == 'ETH' else s
    col_price = 1.0 if col_sym in ('USDC', 'USDS') else executor.get_token_usd_price(_price_sym(col_sym))
    bor_price = 1.0 if bor_sym in ('USDC', 'USDS') else executor.get_token_usd_price(_price_sym(bor_sym))

    col_wei = int(p['collateral_amount_wei'])
    if collateral_usd > 0 and col_price > 0:
        col_wei = int(collateral_usd / col_price * 10**col_dec)
        log.info(f'  collateral_usd override ${collateral_usd:.2f} → {col_wei/10**col_dec:.6f} {col_sym}')

    col_usd   = (col_wei / 10**col_dec) * col_price
    bor_usd   = col_usd * ltv
    bor_wei   = int(bor_usd / bor_price * 10**bor_dec)

    log.info(
        f'fluid_borrow open [{p.get("display_name", col_sym+"->"+ bor_sym)}]: '
        f'LTV={ltv:.1%}  col={col_wei/10**col_dec:.6f} {col_sym} (${col_usd:.2f})  '
        f'borrow={bor_wei/10**bor_dec:.6f} {bor_sym} (${bor_usd:.2f})'
    )

    # 1. Acquire collateral
    _acquire_col(col_sym, col_addr, col_wei)

    # 2. Approve if ERC20 collateral
    if not _is_eth(col_addr):
        executor._approve_if_needed(
            Web3.to_checksum_address(col_addr),
            Web3.to_checksum_address(vault_addr),
            col_wei,
        )

    if not DRY_RUN:
        time.sleep(4)

    # 3. operate(nftId=0, newCol=+col_wei, newDebt=+bor_wei, to=wallet)
    vc = _vault(vault_addr)
    tx = vc.functions.operate(
        0, col_wei, bor_wei, executor.WALLET
    ).build_transaction(executor._tx_params(
        value=col_wei if _is_eth(col_addr) else 0
    ))
    try:
        tx['gas'] = executor._gas_limit(tx)
    except Exception:
        tx['gas'] = 600_000
        log.warning('fluid_borrow open: estimate_gas failed — fallback 600000')

    borrow_txh = executor._send(tx)
    log.info(f'fluid_borrow: operated tx={borrow_txh}')
    try:
        import step_logger as _sl
        _sl.slog('operate', f'{col_sym}→{bor_sym}  TX {borrow_txh[:10]}...', txhash=borrow_txh)
    except Exception:
        pass

    # 4. Extract NFT ID
    nft_id = 0
    if not DRY_RUN:
        time.sleep(4)
        nft_id = _extract_nft_from_receipt(borrow_txh)
        if nft_id == 0:
            log.warning('fluid_borrow: NFT ID=0, position tracking may fail on close')

    # 5. Convert borrowed token → ETH immediately
    if not DRY_RUN:
        time.sleep(4)
    _release_to_eth(bor_sym, bor_addr, bor_wei)

    encoded = encode_state(nft_id, col_sym, col_wei, bor_sym, bor_wei)
    return encoded, borrow_txh


# ── Close borrow ──────────────────────────────────────────────────────────────

def close_borrow(encoded: str, p: dict) -> str:
    """
    Close Fluid T1 borrow position.

    Returns last TX hash.

    Flow:
      1. Parse state → nft_id, col_wei, bor_wei
      2. Acquire repay token (bor_wei × REPAY_BUFFER)
         - ERC20 debt: swap ETH → borrow token → approve vault
         - ETH debt  : use existing wallet ETH (+ guard)
      3. operate(nftId, -(col_wei), -(repay_wei), wallet)
         - If ETH debt: send value=repay_wei
      4. Convert collateral → ETH (returned from vault after close)
      5. Return any surplus borrow token → ETH (best-effort)
    """
    executor._guard()

    s           = parse_state(encoded)
    nft_id      = s['nft_id']
    col_sym     = s['col_sym']
    col_wei     = s['col_wei']
    bor_sym     = s['bor_sym']
    bor_wei     = s['bor_wei']

    vault_addr  = p['vault_address']
    col_addr    = p['collateral_address']
    bor_addr    = p['borrow_address']
    bor_dec     = int(p['borrow_decimals'])

    repay_wei = int(bor_wei * REPAY_BUFFER)

    log.info(
        f'fluid_borrow close [{p.get("display_name", col_sym+"->"+ bor_sym)}]: '
        f'nftId={nft_id}  repay={repay_wei/10**bor_dec:.6f} {bor_sym}'
    )

    # 2. Acquire repay token
    if _is_eth(bor_addr):
        # ETH debt: wallet already holds ETH — just guard
        if not DRY_RUN:
            bal = executor.w3.eth.get_balance(executor.WALLET)
            if bal < repay_wei:
                raise RuntimeError(
                    f'fluid_borrow close: wallet ETH {bal/1e18:.5f} < repay {repay_wei/1e18:.5f}'
                )
    else:
        # ERC20 debt: swap ETH → borrow token
        if DRY_RUN:
            log.info(f'[DRY RUN] swap ETH → {bor_sym} {repay_wei} for repay')
        else:
            try:
                _swap.attempt_swap(
                    _swap.swap_eth_to_token,
                    Web3.to_checksum_address(bor_addr), repay_wei
                )
            except (PriceGuardError, ConfigError, SwapExecutionError) as e:
                raise RuntimeError(f'fluid_borrow close: ETH→{bor_sym} failed: {e}')
            time.sleep(4)
            # Approve vault to spend repay tokens
            executor._approve_if_needed(
                Web3.to_checksum_address(bor_addr),
                Web3.to_checksum_address(vault_addr),
                repay_wei,
            )

    if not DRY_RUN:
        time.sleep(4)

    # 3. operate(nftId, INT256_MIN, INT256_MIN, wallet)
    # INT256_MIN is Fluid's sentinel meaning "withdraw/repay everything".
    # Using exact amounts fails if vault calculates debt slightly different from stored.
    # For ETH debt: msg.value must be >= actual debt (stored × REPAY_BUFFER covers it).
    vc = _vault(vault_addr)
    tx = vc.functions.operate(
        nft_id,
        INT256_MIN,    # withdraw all collateral
        INT256_MIN,    # repay all debt
        executor.WALLET,
    ).build_transaction(executor._tx_params(
        value=repay_wei if _is_eth(bor_addr) else 0
    ))
    try:
        tx['gas'] = executor._gas_limit(tx)
    except Exception:
        tx['gas'] = 800_000
        log.warning('fluid_borrow close: estimate_gas failed — fallback 800000')

    close_txh = executor._send(tx)
    log.info(f'fluid_borrow: closed tx={close_txh}')

    if not DRY_RUN:
        time.sleep(4)

    # 4. Convert returned collateral → ETH
    _release_to_eth(col_sym, col_addr, col_wei)

    if not DRY_RUN:
        time.sleep(4)

    # 5. Convert any leftover borrow token → ETH (from REPAY_BUFFER surplus)
    if not _is_eth(bor_addr):
        _release_to_eth(bor_sym, bor_addr, repay_wei)

    return close_txh
