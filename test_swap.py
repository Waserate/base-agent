"""
Test swap: 0.0003 ETH -> USDC (~$1) via Uniswap v3 on Base.
One-shot verification that wallet + key work.
"""
import os
from web3 import Web3
from dotenv import load_dotenv
import executor

load_dotenv()

UNISWAP_ROUTER = Web3.to_checksum_address('0x2626664c2603336E57B271c5C0b26F421741e481')
WETH_ADDR      = Web3.to_checksum_address('0x4200000000000000000000000000000000000006')
USDC_ADDR      = Web3.to_checksum_address('0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913')
WALLET         = Web3.to_checksum_address(os.getenv('WALLET_ADDRESS'))
PRIVATE_KEY    = os.getenv('WALLET_PRIVATE_KEY')

ROUTER_ABI = [{
    "name": "exactInputSingle",
    "type": "function",
    "stateMutability": "payable",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn",           "type": "address"},
        {"name": "tokenOut",          "type": "address"},
        {"name": "fee",               "type": "uint24"},
        {"name": "recipient",         "type": "address"},
        {"name": "amountIn",          "type": "uint256"},
        {"name": "amountOutMinimum",  "type": "uint256"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"},
    ]}],
    "outputs": [{"name": "amountOut", "type": "uint256"}],
}]

w3 = executor.w3

eth_bal  = executor.get_eth_balance()
usdc_bal = executor.get_token_balance(USDC_ADDR, decimals=6)
print(f'Wallet : {WALLET}')
print(f'ETH    : {eth_bal:.6f}')
print(f'USDC   : {usdc_bal:.4f}')

if eth_bal < 0.001:
    print('\nERROR: ETH too low (need >0.001 for gas + swap)')
    exit(1)

AMOUNT_IN = Web3.to_wei(0.0003, 'ether')
print(f'\nSwapping {Web3.from_wei(AMOUNT_IN, "ether")} ETH -> USDC ...')

router = w3.eth.contract(address=UNISWAP_ROUTER, abi=ROUTER_ABI)
params = (WETH_ADDR, USDC_ADDR, 500, WALLET, AMOUNT_IN, 0, 0)

tx = router.functions.exactInputSingle(params).build_transaction({
    'from'    : WALLET,
    'value'   : AMOUNT_IN,
    'nonce'   : executor._nonce(),
    'gasPrice': executor._gas_price(),
})
tx['gas'] = int(w3.eth.estimate_gas(tx) * 1.2)

signed  = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
txh     = w3.eth.send_raw_transaction(signed.raw_transaction)
print(f'TX     : {txh.hex()}')

receipt = w3.eth.wait_for_transaction_receipt(txh, timeout=60)
status  = 'OK' if receipt.status == 1 else 'FAILED'
print(f'Status : {status}')

eth_after  = executor.get_eth_balance()
usdc_after = executor.get_token_balance(USDC_ADDR, decimals=6)
print(f'\nAfter:')
print(f'ETH    : {eth_after:.6f}  (d {eth_after - eth_bal:+.6f})')
print(f'USDC   : {usdc_after:.4f}  (d {usdc_after - usdc_bal:+.4f})')
print(f'\nhttps://basescan.org/tx/{txh.hex()}')
