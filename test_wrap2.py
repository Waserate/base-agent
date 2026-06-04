"""Test wrap with receipt verification."""
import executor, time
from web3 import Web3
from dotenv import load_dotenv
load_dotenv()

executor.reset_nonce()
WETH_ADDR = '0x4200000000000000000000000000000000000006'
WETH_ABI = [
    {"name": "deposit", "type": "function", "stateMutability": "payable", "inputs": [], "outputs": []},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
]

weth_c = executor.w3.eth.contract(address=Web3.to_checksum_address(WETH_ADDR), abi=WETH_ABI)

nonce_before = executor.w3.eth.get_transaction_count(executor.WALLET, 'latest')
weth_before = weth_c.functions.balanceOf(executor.WALLET).call()
print(f'nonce_confirmed={nonce_before}  WETH_before={weth_before/1e18:.5f}')

# Build wrap TX manually
tx = weth_c.functions.deposit().build_transaction({
    'from': executor.WALLET,
    'nonce': executor._nonce(),
    'gasPrice': executor._gas_price(),
    'gas': 60_000,
    'value': int(0.001 * 1e18),
    'chainId': 8453,
})
print(f'TX nonce={tx["nonce"]}  gasPrice={tx["gasPrice"]/1e9:.4f} gwei')

signed = executor.w3.eth.account.sign_transaction(tx, executor.PRIVATE_KEY)
txh = executor.w3.eth.send_raw_transaction(signed.raw_transaction)
print(f'TX submitted: {txh.hex()}')

r = executor.w3.eth.wait_for_transaction_receipt(txh, timeout=60)
print(f'Receipt: status={r.status}  block={r.blockNumber}  gasUsed={r.gasUsed}')

# Verify on chain
try:
    tx_check = executor.w3.eth.get_transaction(txh)
    print(f'TX on chain: blockNumber={tx_check.blockNumber}')
except Exception as e:
    print(f'TX NOT on chain: {e}')

nonce_after = executor.w3.eth.get_transaction_count(executor.WALLET, 'latest')
weth_after = weth_c.functions.balanceOf(executor.WALLET).call()
print(f'nonce_confirmed={nonce_after}  WETH_after={weth_after/1e18:.5f}')
print(f'WETH delta: {(weth_after-weth_before)/1e18:.5f}')
