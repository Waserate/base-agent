"""Test wrap ETH → WETH and verify balance actually changed."""
import executor, swap, time
from web3 import Web3
from dotenv import load_dotenv
load_dotenv()

executor.reset_nonce()
WETH = '0x4200000000000000000000000000000000000006'
weth_c = executor.w3.eth.contract(address=Web3.to_checksum_address(WETH), abi=executor.ERC20_ABI)

eth_before = executor.get_eth_balance()
weth_before = weth_c.functions.balanceOf(executor.WALLET).call()
print(f'Before: ETH={eth_before:.5f}  WETH={weth_before/1e18:.5f}')

amt = int(0.001 * 1e18)
print(f'Wrapping 0.001 ETH...')
swap.wrap_eth(amt)

eth_after = executor.get_eth_balance()
weth_after = weth_c.functions.balanceOf(executor.WALLET).call()
print(f'After:  ETH={eth_after:.5f}  WETH={weth_after/1e18:.5f}')
print(f'WETH gained: {(weth_after-weth_before)/1e18:.5f}')

if weth_after > weth_before:
    print('WRAP OK - unwrapping back...')
    swap.unwrap_all_weth()
    print(f'Final ETH: {executor.get_eth_balance():.5f}')
else:
    print('WRAP FAILED - WETH not in wallet!')
