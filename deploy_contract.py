"""
deploy_contract.py — deploy minimal ERC20 to Base chain for on-chain activity.

Usage:
    python deploy_contract.py           # deploy 1 contract with random token
    python deploy_contract.py --count 3 # deploy 3 contracts
    DRY_RUN=true python deploy_contract.py

Records each deployment in state.db (platform=deploy_contract, token=SYMBOL, amount_wei=deployed_address).
No withdraw needed — expiry set to 30 days but agent skips this type.
"""

import os, sys, time, random, logging, argparse
from datetime import date, timedelta
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(message)s',
    datefmt='%H:%M:%S',
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

RPC_URL     = os.getenv('BASE_RPC_URL', 'https://mainnet.base.org')
PRIVATE_KEY = os.getenv('WALLET_PRIVATE_KEY')
WALLET      = Web3.to_checksum_address(os.getenv('WALLET_ADDRESS'))
DRY_RUN     = os.getenv('DRY_RUN', '').lower() in ('1', 'true', 'yes')

w3 = Web3(Web3.HTTPProvider(RPC_URL))

SOLIDITY_VERSION = '0.8.20'

CONTRACT_SOURCE = '''
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract FarmToken {
    string public name;
    string public symbol;
    uint8 public constant decimals = 18;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    event Transfer(address indexed from, address indexed to, uint256 value);

    constructor(string memory _name, string memory _symbol, uint256 _supply) {
        name = _name;
        symbol = _symbol;
        totalSupply = _supply * 10**18;
        balanceOf[msg.sender] = totalSupply;
        emit Transfer(address(0), msg.sender, totalSupply);
    }
}
'''

# ── random token params ───────────────────────────────────────────────────────

_PREFIXES = ['Alpha', 'Beta', 'Delta', 'Gamma', 'Nova', 'Apex', 'Flux', 'Volt',
             'Neon', 'Core', 'Edge', 'Peak', 'Zen', 'Arc', 'Vex', 'Ori', 'Axon']
_SUFFIXES = ['Fi', 'Labs', 'Net', 'Hub', 'X', 'Dao', 'Pro', 'Base', 'One', 'Io']

def _random_token():
    prefix = random.choice(_PREFIXES)
    suffix = random.choice(_SUFFIXES)
    name   = f'{prefix}{suffix}'
    symbol = (prefix[:3] + suffix[:1]).upper()
    supply = random.choice([1_000_000, 5_000_000, 10_000_000, 50_000_000, 100_000_000])
    return name, symbol, supply

# ── compiler ──────────────────────────────────────────────────────────────────

def _get_bytecode_abi():
    from solcx import compile_source, install_solc, get_installed_solc_versions
    installed = [str(v) for v in get_installed_solc_versions()]
    if SOLIDITY_VERSION not in installed:
        log.info(f'Installing solc {SOLIDITY_VERSION} (first run, ~30s)...')
        install_solc(SOLIDITY_VERSION, show_progress=True)
    compiled = compile_source(
        CONTRACT_SOURCE,
        output_values=['abi', 'bin'],
        solc_version=SOLIDITY_VERSION,
    )
    key = '<stdin>:FarmToken'
    return compiled[key]['bin'], compiled[key]['abi']

# ── nonce ─────────────────────────────────────────────────────────────────────

_local_nonce = None

def _nonce():
    global _local_nonce
    if _local_nonce is None:
        _local_nonce = w3.eth.get_transaction_count(WALLET, 'pending')
    n = _local_nonce
    _local_nonce += 1
    return n

def _gas_price():
    return int(w3.eth.gas_price * 3)

# ── deploy one contract ───────────────────────────────────────────────────────

def deploy_one() -> tuple[str, str, str, str]:
    """Returns (tx_hash, contract_address, name, symbol)."""
    name, symbol, supply = _random_token()
    log.info(f'Deploying {name} ({symbol}) supply={supply:,}')

    bytecode, abi = _get_bytecode_abi()
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)

    tx = contract.constructor(name, symbol, supply).build_transaction({
        'from':     WALLET,
        'nonce':    _nonce(),
        'gasPrice': _gas_price(),
        'gas':      2_000_000,
        'chainId':  8453,
    })

    # estimate gas
    try:
        estimated = int(w3.eth.estimate_gas(tx) * 1.3)
        tx['gas'] = estimated
        log.info(f'  gas estimated: {estimated:,}')
    except Exception as e:
        tx['gas'] = 500_000
        log.warning(f'  estimate_gas failed ({e}), using 500_000')

    if DRY_RUN:
        log.info(f'  [DRY RUN] SKIP TX  name={name}  symbol={symbol}')
        return '0x' + 'dd' * 32, '0x' + '00' * 20, name, symbol

    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    txh    = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    log.info(f'  TX sent: {txh}')

    receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=90)
    if receipt.status != 1:
        raise RuntimeError(f'Deploy TX reverted: {txh}')

    addr = receipt.contractAddress
    gas_used = receipt.gasUsed
    cost_eth = gas_used * receipt.effectiveGasPrice / 1e18
    log.info(f'  Deployed: {addr}  gas={gas_used:,}  cost={cost_eth:.6f} ETH')
    try:
        import step_logger as _sl
        _sl.slog('deploy', f'{name} ({symbol}) at {addr[:10]}...  TX {txh[:10]}...', txhash=txh)
    except Exception:
        pass
    return txh, addr, name, symbol

# ── state.db ─────────────────────────────────────────────────────────────────

def _record(tx_hash: str, contract_addr: str, symbol: str):
    import state
    state.init_db()
    today  = date.today().isoformat()
    expiry = (date.today() + timedelta(days=30)).isoformat()
    import sqlite3
    with sqlite3.connect(state.DB_PATH) as c:
        c.execute(
            'INSERT INTO positions (platform,token,amount_wei,entry_date,expiry_date,tx_hash,status) '
            'VALUES (?,?,?,?,?,?,?)',
            ('deploy_contract', symbol, contract_addr, today, expiry, tx_hash, 'closed'),
        )
    log.info(f'  Recorded in state.db (platform=deploy_contract token={symbol})')

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--count', type=int, default=1, help='Number of contracts to deploy')
    args = parser.parse_args()

    if not w3.is_connected():
        log.error('RPC not connected'); sys.exit(1)

    eth_bal = w3.eth.get_balance(WALLET)
    log.info(f'Wallet: {WALLET}')
    log.info(f'ETH balance: {eth_bal / 1e18:.5f}')
    log.info(f'Deploying {args.count} contract(s) on Base  dry_run={DRY_RUN}')

    import step_logger as _sl
    _sl.set_context('deploy_contract', 'Deploy ERC20')

    for i in range(args.count):
        if args.count > 1:
            log.info(f'--- Contract {i+1}/{args.count} ---')
        try:
            txh, addr, name, symbol = deploy_one()
            _record(txh, addr, symbol)
            _sl.slog('ok', f'{name} ({symbol})')
        except Exception as e:
            log.error(f'Deploy failed: {e}')
            try:
                _sl.slog('fail', str(e)[:100])
            except Exception:
                pass
            if args.count > 1:
                log.info('Continuing...')
        if i < args.count - 1:
            time.sleep(3)  # brief pause between deploys

    log.info('Done.')

if __name__ == '__main__':
    main()
