"""
megapot_claim.py — check for Megapot wins and claim if any.

Flow:
  1. Query Megapot API for unclaimed wins for this wallet
  2. If wins found: log prize details, call claimWinnings on-chain
  3. Prize (USDC) lands in wallet immediately; ticket NFTs burned

Usage:
    python megapot_claim.py              # check + auto-claim
    DRY_RUN=true python megapot_claim.py # check only, skip TX
"""

import os, sys, time, logging
import requests
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

JACKPOT_ADDR = Web3.to_checksum_address('0x3bAe643002069dBCbcd62B1A4eb4C4A397d042a2')
API_BASE     = 'https://api.megapot.io/v1'

JACKPOT_ABI = [
    {
        "name": "claimWinnings",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "_userTicketIds", "type": "uint256[]"}],
        "outputs": [],
    },
]

# ── nonce / gas ───────────────────────────────────────────────────────────────

_local_nonce: int | None = None

def _nonce() -> int:
    global _local_nonce
    if _local_nonce is None:
        _local_nonce = w3.eth.get_transaction_count(WALLET, 'pending')
    n = _local_nonce
    _local_nonce += 1
    return n

def _gas_price() -> int:
    return int(w3.eth.gas_price * 3)

def _send(tx: dict) -> str:
    if DRY_RUN:
        log.info(f'[DRY RUN] SKIP TX  to={tx.get("to","?")}  gas={tx.get("gas")}')
        return '0x' + 'dd' * 32
    signed  = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    txh     = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=120)
    if receipt.status != 1:
        raise RuntimeError(f'TX reverted: {txh.hex()}')
    return txh.hex()

# ── API ───────────────────────────────────────────────────────────────────────

def fetch_unclaimed_wins(address: str) -> list[dict]:
    """Return all unclaimed winning tickets for address (handles pagination)."""
    wins = []
    url  = f'{API_BASE}/wallets/{address}/wins?claimed=false'

    while url:
        log.info(f'Querying: {url}')
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f'Megapot API error: {e}')

        body = resp.json()
        page = body.get('data', [])
        wins.extend(page)
        log.info(f'  got {len(page)} wins (total so far: {len(wins)})')

        # handle pagination
        if body.get('has_more'):
            cursor = body.get('next_cursor') or body.get('cursor')
            url = f'{API_BASE}/wallets/{address}/wins?claimed=false&cursor={cursor}' if cursor else None
        else:
            url = None

    return wins

# ── claim ─────────────────────────────────────────────────────────────────────

def claim_wins(ticket_ids: list[int]) -> str:
    jackpot = w3.eth.contract(address=JACKPOT_ADDR, abi=JACKPOT_ABI)
    log.info(f'Claiming {len(ticket_ids)} ticket(s): {ticket_ids}')

    tx = jackpot.functions.claimWinnings(ticket_ids).build_transaction({
        'from':     WALLET,
        'nonce':    _nonce(),
        'gasPrice': _gas_price(),
        'gas':      2_000_000,
        'chainId':  8453,
    })

    try:
        tx['gas'] = int(w3.eth.estimate_gas(tx) * 1.5)
        log.info(f'gas estimated: {tx["gas"]:,}')
    except Exception as e:
        tx['gas'] = 500_000
        log.warning(f'estimate_gas failed ({e}) — fallback gas=500000')

    txh = _send(tx)
    log.info(f'claimWinnings TX: {txh}')
    return txh

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    if not w3.is_connected():
        log.error('RPC not connected'); sys.exit(1)

    log.info(f'Wallet:  {WALLET}')
    log.info(f'DRY_RUN: {DRY_RUN}')

    wins = fetch_unclaimed_wins(WALLET)

    if not wins:
        log.info('No unclaimed wins found.')
        return

    log.info(f'Found {len(wins)} unclaimed win(s):')
    total_usdc = 0
    ticket_ids = []

    for w_entry in wins:
        tid        = int(w_entry['ticket_id'])
        round_id   = w_entry.get('round_id', '?')
        amount_wei = int(w_entry.get('winnings_amount', 0))
        amount_usd = amount_wei / 1e6
        total_usdc += amount_usd
        ticket_ids.append(tid)
        log.info(f'  ticket_id={tid}  round={round_id}  prize=${amount_usd:.4f} USDC')

    log.info(f'Total prize: ${total_usdc:.4f} USDC')

    if DRY_RUN:
        log.info('[DRY RUN] Would claim ticket_ids=' + str(ticket_ids))
        return

    txh = claim_wins(ticket_ids)
    time.sleep(3)

    # verify USDC received
    from megapot import ERC20_ABI, USDC_ADDR
    usdc = w3.eth.contract(address=USDC_ADDR, abi=ERC20_ABI)
    bal  = usdc.functions.balanceOf(WALLET).call()
    log.info(f'USDC balance after claim: {bal / 1e6:.4f}')
    log.info(f'Done.  claimed={len(ticket_ids)} tickets  tx={txh}')

if __name__ == '__main__':
    main()
