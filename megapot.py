"""
megapot.py — buy 1 Megapot ticket on Base chain.

Payment cycles through 5 modes (by count % 5):
  0 ETH   : swap ETH -> USDC -> buy
  1 USDC  : USDC direct -> buy
  2 cbBTC : buy cbBTC, sell cbBTC -> ETH, swap ETH -> USDC -> buy
  3 EURC  : buy EURC, sell EURC -> ETH, swap ETH -> USDC -> buy
  4 AERO  : buy AERO, sell AERO -> ETH, swap ETH -> USDC -> buy

Ticket numbers randomly generated from live contract ranges.

Usage:
    python megapot.py
    DRY_RUN=true python megapot.py
"""

import os, sys, time, random, logging, sqlite3
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

USDC_ADDR    = Web3.to_checksum_address('0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')
JACKPOT_ADDR = Web3.to_checksum_address('0x3bAe643002069dBCbcd62B1A4eb4C4A397d042a2')
TICKET_PRICE = 1_000_000  # 1 USDC (6 decimals)

PAYMENT_MODES = ['ETH', 'USDC', 'cbBTC', 'EURC', 'AERO']

# How much of each token to buy before selling back -> ETH -> USDC.
# Target ~$2 worth so we always have enough ETH to buy $1 USDC after selling.
TOKEN_VIA_CONFIG = {
    'cbBTC': {
        'addr':   Web3.to_checksum_address('0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf'),
        'amount': 2_000,        # 8 dec  ≈ 0.00002 BTC ≈ $2.00 at $100k/BTC
        'dec':    8,
    },
    'EURC': {
        'addr':   Web3.to_checksum_address('0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42'),
        'amount': 1_900_000,    # 6 dec  = 1.9 EURC ≈ $2.07
        'dec':    6,
    },
    'AERO': {
        'addr':   Web3.to_checksum_address('0x940181a94A35A4569E4529A3CDfB74e38FD98631'),
        'amount': int(2.0e18),  # 18 dec = 2.0 AERO ≈ $2.00
        'dec':    18,
    },
}

# ── ABIs ──────────────────────────────────────────────────────────────────────

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

JACKPOT_ABI = [
    {
        "name": "buyTickets",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "_tickets",
                "type": "tuple[]",
                "components": [
                    {"name": "normals",   "type": "uint8[]"},
                    {"name": "bonusball", "type": "uint8"},
                ],
            },
            {"name": "_recipient",     "type": "address"},
            {"name": "_referrers",     "type": "address[]"},
            {"name": "_referralSplit", "type": "uint256[]"},
            {"name": "_source",        "type": "bytes32"},
        ],
        "outputs": [{"name": "ticketIds", "type": "uint256[]"}],
    },
    {"name": "normalBallMax",    "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "bonusballMin",     "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "bonusballHardCap", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "currentDrawingId", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "ticketPrice",      "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
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

def _tx_params(**extra) -> dict:
    return {'from': WALLET, 'nonce': _nonce(), 'gasPrice': _gas_price(), 'gas': 2_000_000, **extra}

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

def _approve_usdc(spender: str, amount: int):
    usdc = w3.eth.contract(address=USDC_ADDR, abi=ERC20_ABI)
    if usdc.functions.allowance(WALLET, spender).call() >= amount:
        return
    log.info('Approving USDC for Jackpot...')
    tx = usdc.functions.approve(spender, 2**256 - 1).build_transaction(_tx_params())
    try:
        tx['gas'] = int(w3.eth.estimate_gas(tx) * 1.5)
    except Exception:
        tx['gas'] = 100_000
    _send(tx)
    if not DRY_RUN:
        for _ in range(6):
            time.sleep(1)
            if usdc.functions.allowance(WALLET, spender).call() >= amount:
                return
        log.warning('Allowance not confirmed after 6s — proceeding anyway')

# ── payment mode ──────────────────────────────────────────────────────────────

def _payment_mode() -> str:
    forced = os.getenv('MEGAPOT_MODE', '').upper()
    if forced in PAYMENT_MODES:
        log.info(f'MEGAPOT_MODE override => {forced}')
        return forced
    import state
    state.init_db()
    with sqlite3.connect(state.DB_PATH) as c:
        count = c.execute("SELECT COUNT(*) FROM positions WHERE platform='megapot'").fetchone()[0]
    mode = PAYMENT_MODES[count % len(PAYMENT_MODES)]
    log.info(f'Past megapot entries: {count}  => payment mode: {mode} '
             f'({count % len(PAYMENT_MODES) + 1}/{len(PAYMENT_MODES)})')
    return mode

# ── payment paths ─────────────────────────────────────────────────────────────

def _via_eth():
    """Swap ETH -> USDC directly."""
    import swap
    log.info('ETH mode: swap ETH -> 1 USDC...')
    swap.attempt_swap(swap.swap_eth_to_token, USDC_ADDR, TICKET_PRICE)
    time.sleep(3)
    _log_usdc_bal()

def _via_usdc():
    """Use USDC from wallet."""
    usdc = w3.eth.contract(address=USDC_ADDR, abi=ERC20_ABI)
    bal  = usdc.functions.balanceOf(WALLET).call()
    log.info(f'USDC mode: wallet balance = {bal / 1e6:.4f} USDC')
    if bal >= TICKET_PRICE:
        return
    # not enough — buy via ETH
    log.info('  insufficient USDC — buying via ETH swap...')
    _via_eth()

def _via_token(symbol: str):
    """Buy token, sell back to ETH, buy USDC. Creates diverse on-chain activity."""
    import swap
    cfg  = TOKEN_VIA_CONFIG[symbol]
    addr = cfg['addr']
    amt  = cfg['amount']
    dec  = cfg['dec']

    # step 1: ETH -> token
    log.info(f'{symbol} mode: buying {amt / 10**dec:.6f} {symbol} with ETH...')
    swap.attempt_swap(swap.swap_eth_to_token, addr, amt)
    time.sleep(4)

    # step 2: read actual balance (may differ slightly from requested)
    token    = w3.eth.contract(address=addr, abi=ERC20_ABI)
    tok_bal  = token.functions.balanceOf(WALLET).call() if not DRY_RUN else amt
    log.info(f'  {symbol} balance: {tok_bal / 10**dec:.6f}')

    # step 3: token -> ETH
    log.info(f'  selling {symbol} back to ETH...')
    swap.attempt_swap(swap.swap_token_to_eth, addr, tok_bal)
    time.sleep(4)

    # step 4: ETH -> USDC
    log.info('  ETH -> 1 USDC...')
    swap.attempt_swap(swap.swap_eth_to_token, USDC_ADDR, TICKET_PRICE)
    time.sleep(3)
    _log_usdc_bal()

def _log_usdc_bal():
    if DRY_RUN:
        return
    usdc = w3.eth.contract(address=USDC_ADDR, abi=ERC20_ABI)
    bal  = usdc.functions.balanceOf(WALLET).call()
    log.info(f'USDC balance: {bal / 1e6:.4f}')
    if bal < TICKET_PRICE:
        raise RuntimeError(f'USDC balance {bal} < {TICKET_PRICE} after swap')

# ── random ticket ─────────────────────────────────────────────────────────────

def _get_drawing_ranges(jackpot) -> tuple[int, int, int]:
    """Return (normal_max, bonus_min, bonus_max) for the CURRENT drawing.

    bonusball max is per-drawing (tied to prize pool size) — lives in
    getDrawingState word[10].  bonusballHardCap() is the protocol ceiling,
    not the value the contract validates against on buy.
    """
    normal_max = jackpot.functions.normalBallMax().call()
    bonus_min  = jackpot.functions.bonusballMin().call()

    # getDrawingState raw call — struct layout confirmed 2026-05-29:
    #   word[09] = normalBallMax (uint8)
    #   word[10] = bonusballMax  for current drawing (uint8)
    try:
        sel        = Web3.keccak(text='getDrawingState(uint256)')[:4]
        drawing_id = jackpot.functions.currentDrawingId().call()
        raw        = w3.eth.call({'to': JACKPOT_ADDR, 'data': (sel + drawing_id.to_bytes(32, 'big')).hex()})
        if len(raw) >= 11 * 32:
            bonus_max = int.from_bytes(raw[10 * 32: 11 * 32], 'big')
            log.info(f'Drawing {drawing_id}: normal_max={normal_max}  '
                     f'bonus=[{bonus_min}, {bonus_max}]  (from getDrawingState)')
            return normal_max, bonus_min, bonus_max
    except Exception as e:
        log.warning(f'getDrawingState failed ({e}) — falling back to bonusballHardCap')

    bonus_max = jackpot.functions.bonusballHardCap().call()
    log.info(f'Drawing ranges (fallback): normal_max={normal_max}  bonus=[{bonus_min}, {bonus_max}]')
    return normal_max, bonus_min, bonus_max


def _random_ticket(jackpot) -> dict:
    normal_max, bonus_min, bonus_max = _get_drawing_ranges(jackpot)
    normals   = random.sample(range(1, normal_max + 1), 5)
    bonusball = random.randint(bonus_min, bonus_max)
    log.info(f'Ticket: normals={sorted(normals)}  bonusball={bonusball}')
    return {'normals': normals, 'bonusball': bonusball}

# ── buy ───────────────────────────────────────────────────────────────────────

def buy_ticket() -> tuple[str, str]:
    import step_logger as _sl
    _sl.set_context('megapot', 'Megapot Lottery')
    global TICKET_PRICE
    jackpot = w3.eth.contract(address=JACKPOT_ADDR, abi=JACKPOT_ABI)

    if not DRY_RUN:
        price = jackpot.functions.ticketPrice().call()
        log.info(f'On-chain ticketPrice: {price} USDC wei')
        if price != TICKET_PRICE:
            log.warning(f'ticketPrice changed to {price} — updating')
            TICKET_PRICE = price

    mode = _payment_mode()
    _sl.slog('start', f'mode={mode}  $1 USDC ticket')

    if mode == 'ETH':
        _via_eth()
    elif mode == 'USDC':
        _via_usdc()
    else:
        _via_token(mode)

    _approve_usdc(JACKPOT_ADDR, TICKET_PRICE)

    ticket = _random_ticket(jackpot) if not DRY_RUN else {'normals': [3, 12, 17, 22, 28], 'bonusball': 5}

    log.info('Buying ticket...')
    tx = jackpot.functions.buyTickets(
        [ticket],
        WALLET,
        [],
        [],
        b'\x00' * 32,
    ).build_transaction(_tx_params())

    try:
        tx['gas'] = int(w3.eth.estimate_gas(tx) * 1.5)
        log.info(f'gas estimated: {tx["gas"]:,}')
    except Exception as e:
        tx['gas'] = 1_500_000
        log.warning(f'estimate_gas failed ({e}) — fallback gas=1500000')

    txh = _send(tx)
    log.info(f'buyTickets TX: {txh}')
    try:
        _sl.slog('buy', f'mode={mode}  normals={sorted(ticket["normals"])} bonus={ticket["bonusball"]}  TX {txh[:10]}...', txhash=txh, usd_est=1.0)
    except Exception:
        pass
    return txh, mode

# ── record ────────────────────────────────────────────────────────────────────

def _record(txh: str, mode: str):
    import state
    state.init_db()
    today  = date.today().isoformat()
    expiry = (date.today() + timedelta(days=7)).isoformat()
    with sqlite3.connect(state.DB_PATH) as c:
        c.execute(
            'INSERT INTO positions (platform,token,amount_wei,entry_date,expiry_date,tx_hash,status) '
            'VALUES (?,?,?,?,?,?,?)',
            ('megapot', mode, str(TICKET_PRICE), today, expiry, txh, 'closed'),
        )
    log.info(f'Recorded in state.db (platform=megapot token={mode} amount={TICKET_PRICE})')

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    if not w3.is_connected():
        log.error('RPC not connected'); sys.exit(1)

    eth_bal  = w3.eth.get_balance(WALLET)
    usdc     = w3.eth.contract(address=USDC_ADDR, abi=ERC20_ABI)
    usdc_bal = usdc.functions.balanceOf(WALLET).call()
    log.info(f'Wallet:       {WALLET}')
    log.info(f'ETH balance:  {eth_bal / 1e18:.5f} ETH')
    log.info(f'USDC balance: {usdc_bal / 1e6:.4f} USDC')
    log.info(f'DRY_RUN:      {DRY_RUN}')

    txh, mode = buy_ticket()

    if not DRY_RUN:
        _record(txh, mode)
    else:
        log.info('[DRY RUN] Skipped state.db recording')

    try:
        import step_logger as _sl
        _sl.slog('ok', f'mode={mode}', usd_est=1.0)
    except Exception:
        pass
    log.info(f'Done.  mode={mode}  tx={txh}')

if __name__ == '__main__':
    main()
