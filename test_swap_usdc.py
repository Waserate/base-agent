"""Test swap ETH->USDC directly to isolate STF issue."""
import executor, swap, time
from dotenv import load_dotenv
load_dotenv()

executor.reset_nonce()
time.sleep(5)

USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
from web3 import Web3
usdc_c = executor.w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=executor.ERC20_ABI)

usdc_before = usdc_c.functions.balanceOf(executor.WALLET).call()
eth_before = executor.get_eth_balance()
print(f'Before: ETH={eth_before:.5f}  USDC={usdc_before/1e6:.2f}')
print(f'Nonce will start at: {executor._local_nonce}')

# Swap exactly 2.5 USDC worth of ETH->USDC
target_usdc = 2_500_000
print(f'Swapping ETH -> {target_usdc/1e6} USDC...')
try:
    txh = swap.attempt_swap(swap.swap_eth_to_token, USDC, target_usdc)
    print(f'SWAP OK: {txh[:22]}...')
except Exception as e:
    print(f'SWAP FAIL: {e}')

time.sleep(3)
usdc_after = usdc_c.functions.balanceOf(executor.WALLET).call()
eth_after = executor.get_eth_balance()
print(f'After:  ETH={eth_after:.5f}  USDC={usdc_after/1e6:.2f}')
print(f'USDC gained: {(usdc_after-usdc_before)/1e6:.2f}')
