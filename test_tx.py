"""Quick test: send a 0-value self-transfer to verify Alchemy relays TXs."""
import executor
from dotenv import load_dotenv
load_dotenv()

executor.reset_nonce()
nonce = executor.w3.eth.get_transaction_count(executor.WALLET, 'pending')
gp = executor._gas_price()
print(f'nonce={nonce}  gasPrice={gp} ({gp/1e9:.4f} gwei)')

tx = {
    'from': executor.WALLET,
    'to': executor.WALLET,
    'value': 0,
    'nonce': nonce,
    'gasPrice': gp,
    'gas': 21000,
    'chainId': 8453,
}
signed = executor.w3.eth.account.sign_transaction(tx, executor.PRIVATE_KEY)
txh = executor.w3.eth.send_raw_transaction(signed.raw_transaction)
print(f'TX submitted: {txh.hex()}')
print('Waiting receipt (30s)...')
try:
    r = executor.w3.eth.wait_for_transaction_receipt(txh, timeout=30)
    print(f'SUCCESS status={r.status} block={r.blockNumber}')
except Exception as e:
    print(f'TIMEOUT or error: {e}')
    # Check if it exists
    try:
        tx_check = executor.w3.eth.get_transaction(txh)
        print(f'TX in mempool: nonce={tx_check.nonce}')
    except Exception:
        print('TX not found on chain at all')
