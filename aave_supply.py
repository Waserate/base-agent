"""
aave_supply.py — AAVE v3 supply module for Base chain.

5 platforms (type=aave_supply in contracts.json):
  aave_usdc   — supply USDC   (3.20% APY)
  aave_weth   — supply WETH   (1.56% APY)
  aave_cbbtc  — supply cbBTC  (0.03% APY)
  aave_wsteth — supply wstETH (0.00% APY)
  aave_eurc   — supply EURC   (1.69% APY)

AAVE v3 Pool Base: 0xA238Dd80C259a72e81d7e4664a9801593F98d1c5
"""

import os, logging
from web3 import Web3
from dotenv import load_dotenv
import executor

load_dotenv()
log = logging.getLogger(__name__)

POOL_ADDR   = '0xA238Dd80C259a72e81d7e4664a9801593F98d1c5'
MAX_UINT256 = 2**256 - 1
DRY_RUN     = os.getenv('DRY_RUN', '').lower() in ('1', 'true', 'yes')

_POOL_ABI = [
    {'name': 'supply', 'type': 'function', 'stateMutability': 'nonpayable',
     'inputs': [
         {'name': 'asset',        'type': 'address'},
         {'name': 'amount',       'type': 'uint256'},
         {'name': 'onBehalfOf',   'type': 'address'},
         {'name': 'referralCode', 'type': 'uint16'},
     ], 'outputs': []},
    {'name': 'withdraw', 'type': 'function', 'stateMutability': 'nonpayable',
     'inputs': [
         {'name': 'asset',  'type': 'address'},
         {'name': 'amount', 'type': 'uint256'},
         {'name': 'to',     'type': 'address'},
     ], 'outputs': [{'name': '', 'type': 'uint256'}]},
]

_ATOKEN_ABI = [
    {'name': 'balanceOf', 'type': 'function', 'stateMutability': 'view',
     'inputs': [{'name': 'account', 'type': 'address'}],
     'outputs': [{'name': '', 'type': 'uint256'}]},
]


def supply(asset_addr: str, amount_wei: int) -> str:
    executor._guard()
    pool_addr  = Web3.to_checksum_address(POOL_ADDR)
    asset_addr = Web3.to_checksum_address(asset_addr)
    executor._approve_if_needed(asset_addr, pool_addr, amount_wei)
    pool = executor.w3.eth.contract(address=pool_addr, abi=_POOL_ABI)
    tx = pool.functions.supply(asset_addr, amount_wei, executor.WALLET, 0).build_transaction(
        executor._tx_params()
    )
    try:
        tx['gas'] = executor._gas_limit(tx)
    except Exception:
        tx['gas'] = 400_000
    log.info(f'aave supply  asset={asset_addr}  amount={amount_wei}')
    return executor._send(tx)


def withdraw_all(asset_addr: str) -> str:
    executor._guard()
    pool_addr  = Web3.to_checksum_address(POOL_ADDR)
    asset_addr = Web3.to_checksum_address(asset_addr)
    pool = executor.w3.eth.contract(address=pool_addr, abi=_POOL_ABI)
    tx = pool.functions.withdraw(asset_addr, MAX_UINT256, executor.WALLET).build_transaction(
        executor._tx_params()
    )
    try:
        tx['gas'] = executor._gas_limit(tx)
    except Exception:
        tx['gas'] = 400_000
    log.info(f'aave withdraw_all  asset={asset_addr}')
    return executor._send(tx)


def get_atoken_balance(atoken_addr: str) -> int:
    atoken = executor.w3.eth.contract(
        address=Web3.to_checksum_address(atoken_addr), abi=_ATOKEN_ABI
    )
    return atoken.functions.balanceOf(executor.WALLET).call()
