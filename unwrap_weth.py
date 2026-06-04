"""Emergency: unwrap all WETH back to ETH."""
import state
from swap import unwrap_all_weth, w3, WALLET, WETH_ADDR, executor
from web3 import Web3

state.init_db()

weth = w3.eth.contract(address=WETH_ADDR, abi=[
    {"name": "balanceOf","type":"function","stateMutability":"view",
     "inputs":[{"name":"account","type":"address"}],"outputs":[{"name":"","type":"uint256"}]},
])

bal = weth.functions.balanceOf(WALLET).call()
print(f'WETH balance: {Web3.from_wei(bal, "ether"):.6f}')

if bal == 0:
    print('Nothing to unwrap.')
else:
    print('Unwrapping...')
    unwrap_all_weth()
    eth = executor.get_eth_balance()
    print(f'Done. ETH balance now: {eth:.5f}')
