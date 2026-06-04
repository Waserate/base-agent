"""
onchain_recovery.py — Scan on-chain balances for all configured platforms.
Returns structured list of open positions regardless of state.db contents.

Called automatically by agent.py on startup when DB is empty.
Can also be run manually: python onchain_recovery.py
"""
import json, os, time, logging, re
from datetime import date, timedelta
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()
import executor

log = logging.getLogger(__name__)

w3     = executor.w3_read   # use Alchemy RPC for read-only scans (avoids 429 on public RPC)
WALLET = executor.WALLET

with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as f:
    CFG = json.load(f)

# ── ABIs ───────────────────────────────────────────────────────────────────────
_ERC20_ABI = executor.ERC20_ABI
_BAL_ABI   = [{"name":"balanceOf","type":"function","inputs":[{"name":"","type":"address"}],"outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"}]
_VE_ABI    = [
    {"name":"balanceOf",           "type":"function","inputs":[{"name":"","type":"address"}],"outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"},
    {"name":"tokenOfOwnerByIndex", "type":"function","inputs":[{"name":"owner","type":"address"},{"name":"index","type":"uint256"}],"outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"},
    {"name":"locked",              "type":"function","inputs":[{"name":"tokenId","type":"uint256"}],"outputs":[{"name":"amount","type":"int128"},{"name":"end","type":"uint256"}],"stateMutability":"view"},
]
VE_ADDR = '0xeBf418Fe2512e7E6bd9b87a8F0f294aCDC67e6B4'

_BORROW_TYPES = {'compound_borrow', 'mw_borrow', 'fluid_borrow', 'aave_borrow'}

# Borrow types: detection now implemented, but still skip auto-close to avoid
# false-positives from RPC misses. Borrows detected on-chain are ADDED to DB
# (step 1), but borrows in DB not found on-chain are NOT removed (step 2).
_SKIP_AUTO_CLOSE = _BORROW_TYPES

# ABIs for borrow protocol detection
_COMET_ABI = [
    {'name':'borrowBalanceOf',   'type':'function','stateMutability':'view',
     'inputs':[{'name':'account','type':'address'}],'outputs':[{'name':'','type':'uint256'}]},
    {'name':'collateralBalanceOf','type':'function','stateMutability':'view',
     'inputs':[{'name':'account','type':'address'},{'name':'asset','type':'address'}],
     'outputs':[{'name':'','type':'uint128'}]},
]
_CTOKEN_ABI = [
    {'name':'balanceOf',         'type':'function','stateMutability':'view',
     'inputs':[{'name':'account','type':'address'}],'outputs':[{'name':'','type':'uint256'}]},
    {'name':'borrowBalanceStored','type':'function','stateMutability':'view',
     'inputs':[{'name':'account','type':'address'}],'outputs':[{'name':'','type':'uint256'}]},
    {'name':'exchangeRateStored','type':'function','stateMutability':'view',
     'inputs':[],'outputs':[{'name':'','type':'uint256'}]},
]
_ERC721_ABI = [
    {'name':'balanceOf',          'type':'function','stateMutability':'view',
     'inputs':[{'name':'owner','type':'address'}],'outputs':[{'name':'','type':'uint256'}]},
    {'name':'tokenOfOwnerByIndex','type':'function','stateMutability':'view',
     'inputs':[{'name':'owner','type':'address'},{'name':'index','type':'uint256'}],
     'outputs':[{'name':'','type':'uint256'}]},
]

# NFPM ABIs for Uni v3 / PancakeSwap v3 NFT position recovery
_NFPM_ABI = [
    {"name":"balanceOf","type":"function","stateMutability":"view",
     "inputs":[{"name":"owner","type":"address"}],"outputs":[{"name":"","type":"uint256"}]},
    {"name":"tokenOfOwnerByIndex","type":"function","stateMutability":"view",
     "inputs":[{"name":"owner","type":"address"},{"name":"index","type":"uint256"}],
     "outputs":[{"name":"","type":"uint256"}]},
    {"name":"positions","type":"function","stateMutability":"view",
     "inputs":[{"name":"tokenId","type":"uint256"}],
     "outputs":[
         {"name":"nonce","type":"uint96"},{"name":"operator","type":"address"},
         {"name":"token0","type":"address"},{"name":"token1","type":"address"},
         {"name":"fee","type":"uint24"},{"name":"tickLower","type":"int24"},
         {"name":"tickUpper","type":"int24"},{"name":"liquidity","type":"uint128"},
         {"name":"feeGrowthInside0LastX128","type":"uint256"},
         {"name":"feeGrowthInside1LastX128","type":"uint256"},
         {"name":"tokensOwed0","type":"uint128"},{"name":"tokensOwed1","type":"uint128"},
     ]},
]
_UNI_NFPM_ADDR  = '0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1'
_CAKE_NFPM_ADDR = '0x46A15B0b27311cedF172AB29E4f4766fbE7F4364'
_UNI_FACTORY    = '0x33128a8fC17869897dcE68Ed026d694621f6FDfD'
_CAKE_FACTORY   = '0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865'


def _balanceof(addr: str) -> int:
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=_BAL_ABI)
        return c.functions.balanceOf(WALLET).call()
    except Exception:
        return 0


def _scan_compound_borrows(platforms: dict, today: str, found: list, verbose: bool):
    """Detect Compound v3 borrow positions via borrowBalanceOf + collateralBalanceOf."""
    # Group platforms by (comet_addr, borrow_token) to avoid redundant comet calls
    comet_groups: dict = {}
    for key, p in platforms.items():
        if not isinstance(p, dict) or p.get('type') != 'compound_borrow':
            continue
        k = (p['comet_address'].lower(), p['borrow_token'])
        comet_groups.setdefault(k, []).append((key, p))

    for (comet_addr, borrow_token), plat_list in comet_groups.items():
        try:
            comet = w3.eth.contract(address=Web3.to_checksum_address(comet_addr), abi=_COMET_ABI)
            borrow_wei = comet.functions.borrowBalanceOf(WALLET).call()
            time.sleep(0.4)
            if borrow_wei == 0:
                continue

            # Collect all possible collateral assets across platforms in this group
            all_cols: dict = {}   # sym -> config dict
            for _, p in plat_list:
                for c in p.get('collaterals', []):
                    all_cols.setdefault(c['token'], c)

            active_cols = []
            for sym, c in all_cols.items():
                col_bal = comet.functions.collateralBalanceOf(
                    WALLET, Web3.to_checksum_address(c['address'])).call()
                time.sleep(0.3)
                if col_bal > 0:
                    active_cols.append({'token': sym, 'wei': col_bal})

            if not active_cols:
                continue

            # Match to most specific platform by collateral token set
            active_set = frozenset(c['token'] for c in active_cols)
            matched_key = None
            for key, p in plat_list:
                p_set = frozenset(c['token'] for c in p.get('collaterals', []))
                if active_set == p_set:
                    matched_key = key
                    break
            if not matched_key:
                # Partial match — pick platform that covers all active collaterals
                for key, p in plat_list:
                    p_set = frozenset(c['token'] for c in p.get('collaterals', []))
                    if active_set.issubset(p_set):
                        matched_key = key
                        break
            if not matched_key:
                matched_key = plat_list[0][0]   # last resort

            coll_str = '|'.join(f'{c["token"]}:{c["wei"]}' for c in active_cols)
            encoded  = f'{coll_str}||{borrow_token}:{borrow_wei}'
            found.append({'platform': matched_key, 'token': borrow_token,
                          'amount_wei': encoded, 'ptype': 'compound_borrow',
                          'entry_date': today,
                          'expiry_date': _expiry_for('compound_borrow', platform_key=matched_key)})
            if verbose:
                log.info(f'  FOUND {matched_key} (compound_borrow) '
                         f'col={active_set} bor={borrow_token}:{borrow_wei}')
        except Exception as e:
            if verbose:
                log.warning(f'  SKIP compound_borrow {comet_addr[:10]}: {e}')
            time.sleep(0.5)


def _claimed_borrow_tokens(ptype: str, found: list) -> set:
    """
    Build set of borrow_tokens already claimed for a given ptype.

    Shared-pool protocols (Moonwell/AAVE) pool ALL borrows under one token regardless
    of which collateral was used. borrowBalanceStored / vDebt.balanceOf return the
    TOTAL debt for a token — not per-collateral.  If we already know a borrow exists
    (from state.db or earlier in this scan), any other platform config that checks the
    SAME borrow_token will be a FALSE POSITIVE and must be skipped.
    """
    claimed = set()
    # From this scan pass so far
    for r in found:
        if r['ptype'] == ptype:
            claimed.add(r['token'])
    # From state.db (positions our agent opened — highest confidence)
    try:
        import state as _st
        for pos in _st.get_active():
            import json as _js
            with open(os.path.join(os.path.dirname(__file__), 'config/contracts.json')) as _f:
                _cfg = _js.load(_f)
            p_cfg = _cfg.get('platforms', {}).get(pos[1], {})
            if p_cfg.get('type') == ptype:
                claimed.add(p_cfg.get('borrow_token', ''))
    except Exception:
        pass
    return claimed


def _scan_moonwell_borrows(platforms: dict, today: str, found: list, verbose: bool):
    """
    Detect Moonwell borrow positions.

    Deduplication: Moonwell Comptroller pools ALL collateral/debt — borrowBalanceStored
    returns TOTAL debt for a token regardless of which collateral was used.  A simple
    LEND (ctoken supply) and a BORROW using a different collateral both contribute to
    mToken balance and debt balance.  We skip any platform whose borrow_token is already
    claimed by a known position to prevent false positives.
    """
    claimed = _claimed_borrow_tokens('mw_borrow', found)

    for key, p in platforms.items():
        if not isinstance(p, dict) or p.get('type') != 'mw_borrow':
            continue
        col_mtoken  = p.get('collateral_mtoken', '')
        bor_mtoken  = p.get('borrow_mtoken', '')
        bor_addr    = p.get('borrow_address', '')
        col_token   = p.get('collateral_token', '')
        bor_token   = p.get('borrow_token', '')
        if not col_mtoken or not bor_mtoken:
            continue
        # Skip: borrow_token already claimed by another mw_borrow position
        if bor_token in claimed:
            if verbose:
                log.info(f'  SKIP {key} (mw_borrow): borrow_token {bor_token} already claimed')
            continue
        try:
            col_c = w3.eth.contract(address=Web3.to_checksum_address(col_mtoken), abi=_CTOKEN_ABI)
            col_shares = col_c.functions.balanceOf(WALLET).call()
            time.sleep(0.3)
            if col_shares == 0:
                continue
            # Convert mToken shares → underlying via exchangeRateStored (Compound v2 formula)
            exr = col_c.functions.exchangeRateStored().call()
            time.sleep(0.3)
            col_wei = col_shares * exr // 10**18
            bor_c = w3.eth.contract(address=Web3.to_checksum_address(bor_mtoken), abi=_CTOKEN_ABI)
            bor_wei = bor_c.functions.borrowBalanceStored(WALLET).call()
            time.sleep(0.3)
            if bor_wei == 0:
                continue
            encoded = f'{col_token}:{col_wei}||{bor_token}:{bor_wei}:{bor_mtoken}:{bor_addr}'
            found.append({'platform': key, 'token': bor_token,
                          'amount_wei': encoded, 'ptype': 'mw_borrow',
                          'entry_date': today,
                          'expiry_date': _expiry_for('mw_borrow', platform_key=key)})
            claimed.add(bor_token)   # mark this borrow_token as taken
            if verbose:
                log.info(f'  FOUND {key} (mw_borrow) '
                         f'col={col_token}:{col_wei} bor={bor_token}:{bor_wei}')
        except Exception as e:
            if verbose:
                log.warning(f'  SKIP mw_borrow {key}: {e}')
            time.sleep(0.5)


def _scan_aave_borrows(platforms: dict, today: str, found: list, verbose: bool):
    """
    Detect AAVE v3 borrow positions.

    Deduplication: AAVE aToken balance is indistinguishable between simple supply
    (aave_supply) and borrow collateral.  vDebt balance is total debt for a token
    regardless of which collateral was used.  Skip any platform whose borrow_token is
    already claimed to prevent false positives (same pattern as Moonwell).
    """
    claimed = _claimed_borrow_tokens('aave_borrow', found)

    # Build aToken map from aave_supply platforms: token_sym -> atoken_address
    atoken_map: dict = {}
    for _, p in platforms.items():
        if not isinstance(p, dict) or p.get('type') != 'aave_supply':
            continue
        atoken_map[p.get('token', '')] = p.get('atoken_address', '')

    for key, p in platforms.items():
        if not isinstance(p, dict) or p.get('type') != 'aave_borrow':
            continue
        col_token  = p.get('collateral_token', '')
        bor_token  = p.get('borrow_token', '')
        vdebt_addr = p.get('borrow_vdebt', '')
        atoken_addr = atoken_map.get(col_token, '')
        if not atoken_addr or not vdebt_addr:
            continue
        # Skip: borrow_token already claimed by another aave_borrow position
        if bor_token in claimed:
            if verbose:
                log.info(f'  SKIP {key} (aave_borrow): borrow_token {bor_token} already claimed')
            continue
        try:
            col_bal = _balanceof(atoken_addr)
            time.sleep(0.3)
            if col_bal == 0:
                continue
            debt_bal = _balanceof(vdebt_addr)
            time.sleep(0.3)
            if debt_bal == 0:
                continue
            encoded = f'{col_token}:{col_bal}||{bor_token}:{debt_bal}'
            found.append({'platform': key, 'token': bor_token,
                          'amount_wei': encoded, 'ptype': 'aave_borrow',
                          'entry_date': today,
                          'expiry_date': _expiry_for('aave_borrow', platform_key=key)})
            claimed.add(bor_token)   # mark this borrow_token as taken
            if verbose:
                log.info(f'  FOUND {key} (aave_borrow) '
                         f'col={col_token}:{col_bal} bor={bor_token}:{debt_bal}')
        except Exception as e:
            if verbose:
                log.warning(f'  SKIP aave_borrow {key}: {e}')
            time.sleep(0.5)


def _scan_fluid_borrows(platforms: dict, today: str, found: list, verbose: bool):
    """Detect Fluid T1 vault borrow positions via vault ERC721 balanceOf + tokenOfOwnerByIndex."""
    for key, p in platforms.items():
        if not isinstance(p, dict) or p.get('type') != 'fluid_borrow':
            continue
        vault_addr = p.get('vault_address', '')
        if not vault_addr:
            continue
        col_token = p.get('collateral_token', '')
        bor_token = p.get('borrow_token', '')
        try:
            vault = w3.eth.contract(address=Web3.to_checksum_address(vault_addr), abi=_ERC721_ABI)
            nft_count = vault.functions.balanceOf(WALLET).call()
            time.sleep(0.3)
            if nft_count == 0:
                continue

            # Try tokenOfOwnerByIndex (Fluid may not support ERC721Enumerable)
            nft_id = None
            try:
                nft_id = vault.functions.tokenOfOwnerByIndex(WALLET, 0).call()
                time.sleep(0.3)
            except Exception:
                nft_id = None

            if nft_id is None:
                if verbose:
                    log.warning(f'  {key} (fluid_borrow) nft_count={nft_count} '
                                f'but tokenOfOwnerByIndex unsupported — check state.db history')
                # Don't add — can't reconstruct state without nftId
                continue

            # Use config amounts as best-guess (actual amounts may differ)
            col_wei = p.get('collateral_amount_wei', 0)
            bor_wei = 0   # unknown without vault read — will be updated by health_monitor
            encoded = f'nftId:{nft_id}||COL:{col_token}:{col_wei}||BOR:{bor_token}:{bor_wei}'
            found.append({'platform': key, 'token': bor_token,
                          'amount_wei': encoded, 'ptype': 'fluid_borrow',
                          'entry_date': today,
                          'expiry_date': _expiry_for('fluid_borrow', platform_key=key)})
            if verbose:
                log.info(f'  FOUND {key} (fluid_borrow) nftId={nft_id}')
        except Exception:
            time.sleep(0.3)   # Fluid vaults often don't support standard ERC721 — skip silently


def _scan_nfpm_positions(nfpm_addr: str, ptype: str, platforms: dict, today: str,
                          found: list, verbose: bool = False):
    """
    Scan a NonfungiblePositionManager for tokenIds owned by WALLET.
    Matches each live position to a platform key by (token0, token1, fee).
    Adds to `found` list. Used for uni_lp and pancake_lp recovery.
    """
    try:
        nfpm = w3.eth.contract(address=Web3.to_checksum_address(nfpm_addr), abi=_NFPM_ABI)
        count = nfpm.functions.balanceOf(WALLET).call()
        time.sleep(0.3)
    except Exception as e:
        if verbose:
            log.warning(f'  NFPM {nfpm_addr} balanceOf failed: {e}')
        return

    if count == 0:
        return

    # Build lookup: (token0_lower, token1_lower, fee) -> platform_key
    # fee is encoded in key suffix (e.g. uni_lp_weth_usdc_3000 → fee=3000)
    pool_map = {}
    pool_addr_map = {}   # pool_address_lower -> platform_key (fallback)
    for key, p in platforms.items():
        if not isinstance(p, dict):
            continue
        if p.get('type') != ptype:
            continue
        t0 = p.get('token0_address', '').lower()
        t1 = p.get('token1_address', '').lower()
        # Parse fee from key name suffix (e.g. "_3000" → 3000)
        m = re.search(r'_(\d+)$', key)
        fee = int(m.group(1)) if m else None
        if t0 and t1 and fee is not None:
            pool_map[(t0, t1, fee)] = key
            pool_map[(t1, t0, fee)] = key  # order-insensitive
        # Fallback: pool address match
        pa = p.get('pool_address', '').lower()
        if pa:
            pool_addr_map[pa] = key

    if verbose:
        log.info(f'  NFPM {nfpm_addr} ({ptype}): wallet owns {count} position(s)')

    for i in range(count):
        try:
            token_id = nfpm.functions.tokenOfOwnerByIndex(WALLET, i).call()
            time.sleep(0.6)
            pos = nfpm.functions.positions(token_id).call()
            time.sleep(0.6)
            # pos: nonce, operator, token0, token1, fee, tickLower, tickUpper, liquidity, ...
            t0_addr = pos[2].lower()
            t1_addr = pos[3].lower()
            fee      = int(pos[4])
            liquidity = int(pos[7])
            if liquidity == 0:
                continue
            pk = pool_map.get((t0_addr, t1_addr, fee))
            if pk is None:
                # Fallback: derive pool address via factory and compare
                try:
                    _factory = (_UNI_FACTORY if ptype == 'uni_lp' else _CAKE_FACTORY)
                    _fac_abi = [{"name":"getPool","type":"function","stateMutability":"view",
                                 "inputs":[{"name":"tokenA","type":"address"},
                                           {"name":"tokenB","type":"address"},
                                           {"name":"fee","type":"uint24"}],
                                 "outputs":[{"name":"","type":"address"}]}]
                    _fac = w3.eth.contract(address=Web3.to_checksum_address(_factory), abi=_fac_abi)
                    _pool_addr = _fac.functions.getPool(
                        Web3.to_checksum_address(t0_addr),
                        Web3.to_checksum_address(t1_addr), fee).call().lower()
                    pk = pool_addr_map.get(_pool_addr)
                except Exception:
                    pk = None
            if pk is None:
                if verbose:
                    log.warning(f'  NFPM tokenId={token_id} ({t0_addr[:8]}../{t1_addr[:8]}.. fee={fee}) no match in config — skip')
                continue
            found.append({'platform': pk, 'token': 'LP',
                          'amount_wei': str(token_id), 'ptype': ptype,
                          'entry_date': today,
                          'expiry_date': _expiry_for(ptype, platform_key=pk)})
            if verbose:
                log.info(f'  FOUND {pk} ({ptype}) tokenId={token_id} liquidity={liquidity}')
        except Exception as e:
            if verbose:
                log.warning(f'  NFPM tokenId scan [{i}] error: {e}')
            time.sleep(0.4)


def _expiry_for(ptype: str, lock_end_ts: int = 0, platform_key: str = '') -> str:
    """Platform-config expiry. aero_vote uses on-chain lock end timestamp."""
    import random
    if ptype == 'aero_vote' and lock_end_ts:
        from datetime import datetime
        return datetime.utcfromtimestamp(lock_end_ts).strftime('%Y-%m-%d')
    days_range = CFG.get('platforms', {}).get(platform_key, {}).get('expiry_days', [3, 5])
    days = random.randint(int(days_range[0]), int(days_range[1]))
    return (date.today() + timedelta(days=days)).isoformat()


def scan(verbose: bool = False) -> list:
    """
    Scan all platforms in contracts.json for on-chain balances.
    Returns list of dicts compatible with state.restore_from_recovery().

    Each dict: {platform, token, amount_wei, ptype, expiry_date}
    Skips borrow types (can't auto-detect without stored position IDs).
    """
    found = []
    platforms = CFG.get('platforms', {})
    today = date.today().isoformat()

    for key, p in platforms.items():
        if not isinstance(p, dict):
            continue
        ptype = p.get('type', '')
        if not ptype or ptype in _BORROW_TYPES:
            continue

        token = p.get('token', '')
        time.sleep(0.4)

        try:
            # ── ERC4626 (Morpho, Fluid, Beefy single) ──────────────────────
            if ptype in ('erc4626', 'beefy_single'):
                addr = p.get('address', '')
                if not addr:
                    continue
                bal = _balanceof(addr)
                if bal > 0:
                    found.append({'platform': key, 'token': token,
                                  'amount_wei': str(bal), 'ptype': ptype,
                                  'entry_date': today,
                                  'expiry_date': _expiry_for(ptype, platform_key=key)})
                    if verbose:
                        log.info(f'  FOUND {key} ({ptype}) shares={bal}')

            # ── CToken (Moonwell) ───────────────────────────────────────────
            elif ptype == 'ctoken':
                addr = p.get('address', '')
                if not addr:
                    continue
                bal = _balanceof(addr)
                if bal > 0:
                    found.append({'platform': key, 'token': token,
                                  'amount_wei': str(bal), 'ptype': ptype,
                                  'entry_date': today,
                                  'expiry_date': _expiry_for(ptype, platform_key=key)})
                    if verbose:
                        log.info(f'  FOUND {key} ({ptype}) cTokens={bal}')

            # ── Comet supply (Compound v3 supply side) ──────────────────────
            elif ptype == 'comet':
                addr = p.get('address', '')
                if not addr:
                    continue
                bal = _balanceof(addr)
                if bal > 0:
                    found.append({'platform': key, 'token': token,
                                  'amount_wei': str(bal), 'ptype': ptype,
                                  'entry_date': today,
                                  'expiry_date': _expiry_for(ptype, platform_key=key)})
                    if verbose:
                        log.info(f'  FOUND {key} ({ptype}) supplied={bal}')

            # ── PSM hold (Spark sUSDS) ──────────────────────────────────────
            elif ptype == 'psm_hold':
                susds_addr = CFG['tokens'].get('sUSDS', {}).get('address', '')
                if not susds_addr:
                    continue
                bal = _balanceof(susds_addr)
                if bal > 0:
                    found.append({'platform': key, 'token': 'sUSDS',
                                  'amount_wei': str(bal), 'ptype': ptype,
                                  'entry_date': today,
                                  'expiry_date': _expiry_for(ptype, platform_key=key)})
                    if verbose:
                        log.info(f'  FOUND {key} ({ptype}) sUSDS={bal}')

            # ── Beefy LP ────────────────────────────────────────────────────
            elif ptype == 'beefy_lp':
                addr = p.get('address', '')
                if not addr:
                    continue
                bal = _balanceof(addr)
                if bal > 0:
                    found.append({'platform': key, 'token': token,
                                  'amount_wei': str(bal), 'ptype': ptype,
                                  'entry_date': today,
                                  'expiry_date': _expiry_for(ptype, platform_key=key)})
                    if verbose:
                        log.info(f'  FOUND {key} ({ptype}) shares={bal}')

            # ── Aero LP (gauge staked or unstaked) ─────────────────────────
            elif ptype == 'aero_lp':
                lp_token  = f"{p.get('token0','?')}/{p.get('token1','?')}"
                gauge_addr = p.get('gauge_address', '')
                pool_addr  = p.get('pool_address', '')
                # Check staked (gauge) first, then unstaked (pool LP token)
                bal = _balanceof(gauge_addr) if gauge_addr else 0
                source = 'gauge'
                if bal == 0 and pool_addr:
                    bal    = _balanceof(pool_addr)
                    source = 'unstaked'
                if bal > 0:
                    found.append({'platform': key, 'token': lp_token,
                                  'amount_wei': str(bal), 'ptype': ptype,
                                  'entry_date': today,
                                  'expiry_date': _expiry_for(ptype, platform_key=key)})
                    if verbose:
                        log.info(f'  FOUND {key} ({ptype}) {source}_lp={bal}')

            # ── Uniswap v3 / PancakeSwap v3 LP: handled via NFPM batch below ──
            elif ptype in ('uni_lp', 'pancake_lp'):
                pass  # collected in bulk at end of loop via _scan_nfpm_positions

            # ── AAVE supply ─────────────────────────────────────────────────
            elif ptype == 'aave_supply':
                atoken_addr = p.get('atoken_address', '')
                if not atoken_addr:
                    continue
                bal = _balanceof(atoken_addr)
                if bal > 0:
                    found.append({'platform': key, 'token': token,
                                  'amount_wei': str(bal), 'ptype': ptype,
                                  'entry_date': today,
                                  'expiry_date': _expiry_for(ptype, platform_key=key)})
                    if verbose:
                        log.info(f'  FOUND {key} ({ptype}) aToken={bal}')

        except Exception as e:
            if verbose:
                log.warning(f'  SKIP {key}: {e}')
            time.sleep(0.5)  # extra wait after error
            continue

    # ── Uniswap v3 / PancakeSwap v3 LP: enumerate NFPM tokenIds ──────────────
    _scan_nfpm_positions(_UNI_NFPM_ADDR,  'uni_lp',     platforms, today, found, verbose)
    _scan_nfpm_positions(_CAKE_NFPM_ADDR, 'pancake_lp', platforms, today, found, verbose)

    # ── Borrow positions: Compound v3 / Moonwell / AAVE / Fluid ───────────────
    _scan_compound_borrows(platforms, today, found, verbose)
    _scan_moonwell_borrows(platforms, today, found, verbose)
    _scan_aave_borrows(platforms, today, found, verbose)
    _scan_fluid_borrows(platforms, today, found, verbose)

    # ── veAERO: VE contract doesn't support ERC721Enumerable — use ownerOf check ──
    # Build candidate tokenIds from state.db history across ALL wallets,
    # then verify ownership via ownerOf for current WALLET.
    _VE_ABI2 = _VE_ABI + [
        {"name":"ownerOf","type":"function","inputs":[{"name":"tokenId","type":"uint256"}],
         "outputs":[{"name":"","type":"address"}],"stateMutability":"view"},
    ]
    try:
        ve = w3.eth.contract(address=Web3.to_checksum_address(VE_ADDR), abi=_VE_ABI2)
        nft_count = ve.functions.balanceOf(WALLET).call()
        time.sleep(0.4)

        if nft_count > 0:
            if verbose:
                log.info(f'  veAERO: wallet owns {nft_count} NFT(s) — scanning candidates')
            # Collect tokenIds from ALL state.db files we can find
            candidate_ids = set()
            import glob as _glob
            for _db in _glob.glob(os.path.join(os.path.dirname(__file__), 'state*.db')):
                try:
                    import sqlite3 as _sq
                    with _sq.connect(_db) as _c:
                        for _row in _c.execute("SELECT amount_wei FROM positions WHERE platform='aero_vote'").fetchall():
                            raw = str(_row[0])
                            tid = int(raw.split('|')[0]) if '|' in raw else int(raw)
                            candidate_ids.add(tid)
                except Exception:
                    pass
            if not candidate_ids:
                candidate_ids = {120320}  # last-resort fallback (test wallet's known tokenId)

            for token_id in candidate_ids:
                try:
                    owner = ve.functions.ownerOf(token_id).call()
                    time.sleep(0.4)
                    if owner.lower() != WALLET.lower():
                        continue
                    locked = ve.functions.locked(token_id).call()
                    time.sleep(0.4)
                    aero_amt, lock_end = locked[0], locked[1]
                    if aero_amt > 0:
                        amount_str = f'{token_id}|{aero_amt}'
                        found.append({'platform': 'aero_vote', 'token': 'AERO',
                                      'amount_wei': amount_str, 'ptype': 'aero_vote',
                                      'entry_date': today,
                                      'expiry_date': _expiry_for('aero_vote', lock_end)})
                        if verbose:
                            log.info(f'  FOUND aero_vote tokenId={token_id} locked={aero_amt/1e18:.4f} AERO')
                except Exception:
                    time.sleep(0.4)
                    continue
    except Exception as e:
        if verbose:
            log.warning(f'  veAERO scan failed: {e}')

    return found


def reconcile(verbose: bool = True) -> dict:
    """
    Always-on startup reconcile: compare on-chain truth with state.db.

    Actions:
      - on-chain found, not in DB  → insert (recovered)
      - DB active, not on-chain    → mark closed (only non-borrow types)
      - both match                 → no action

    Borrow types are skipped for auto-close (can't reliably detect on-chain).

    Returns dict with counts: added, closed, unchanged.
    """
    import state as _state

    onchain = scan(verbose=verbose)
    onchain_platforms = {r['platform'] for r in onchain}

    db_active = _state.get_active()
    db_map    = {pos[1]: pos for pos in db_active}  # platform → row

    added = 0

    # ── Step 1: on-chain found but not in DB → add ─────────────────────────
    to_add = [r for r in onchain if r['platform'] not in db_map]
    if to_add:
        n = _state.restore_from_recovery(to_add)
        added = n
        if verbose:
            for r in to_add:
                log.info(f'  [RECOVERED] {r["platform"]} — added to DB')

    # ── Step 2: auto-close DISABLED ───────────────────────────────────────────
    # Auto-closing based on scan results is too risky:
    #   - _balanceof() returns 0 silently on any RPC error (429, timeout, network)
    #   - A flaky RPC at startup would close ALL active positions
    #   - Legitimate use case (agent expiry-based close) is handled by maintenance_job
    # reconcile() is recovery-only: it adds missing positions, never removes them.

    if verbose:
        log.info(f'  Reconcile done: +{added} recovered (auto-close disabled)')

    return {'added': added, 'closed': 0, 'skipped': 0}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    log.info(f'Scanning on-chain for wallet {WALLET}...')
    log.info('=' * 60)
    results = scan(verbose=True)
    log.info('=' * 60)
    log.info(f'Found {len(results)} open positions on-chain:')
    for r in results:
        log.info(f"  {r['platform']:<30} {r['token']:<10} expires={r['expiry_date']}")
