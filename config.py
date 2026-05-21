import os
from dotenv import load_dotenv

load_dotenv()

# Chain
CHAIN_ID = 8453
RPC_URL = os.getenv("RPC_URL", "https://mainnet.base.org")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")

# Contracts
ROUTER = "0x5F231AEA5AbD403aF0e8a32c1feF85a9a3ec5622"
HOOK = "0xd5252A67935fc6b913C4441ac0E5EBF3219fAAa8"
POOL_MANAGER = "0x498581fF718922c3f8e6A244956aF099B2652b2b"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
PITCH_TOKEN = "0xeae13ea73bec936664a51734c8c01ec7c3b0699c"
TOKEN_REGISTRY = "0x2dee421d9f41d92c4553035d701ed272261eb9f5"

# ABIs (minimal)
ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "name", "outputs": [{"name": "", "type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalSupply", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]

ROUTER_ABI = [
    {"inputs": [{"name": "player", "type": "address"}, {"name": "countryIn", "type": "uint256"}, {"name": "minOut", "type": "uint256"}], "name": "buy", "outputs": [{"name": "playerOut", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "player", "type": "address"}, {"name": "playerIn", "type": "uint256"}, {"name": "minOut", "type": "uint256"}], "name": "sell", "outputs": [{"name": "countryOut", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "hook", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "poolManager", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
]

# getToken(uint8 countryId) on token registry
REGISTRY_ABI = [
    {"inputs": [{"name": "countryId", "type": "uint8"}], "name": "getToken", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
]

# Hook read methods (verified contract)
HOOK_ABI = [
    {"inputs": [{"name": "player", "type": "address"}], "name": "currentPrice", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "player", "type": "address"}, {"name": "countryIn", "type": "uint256"}], "name": "quoteBuy", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "player", "type": "address"}, {"name": "amountIn", "type": "uint256"}], "name": "quoteSell", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "token", "type": "address"}], "name": "getBaseCurrency", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
]

# Multicall3 ABI
MULTICALL3_ABI = [
    {"inputs": [{"components": [{"name": "target", "type": "address"}, {"name": "allowFailure", "type": "bool"}, {"name": "callData", "type": "bytes"}], "name": "calls", "type": "tuple[]"}], "name": "aggregate3", "outputs": [{"components": [{"name": "success", "type": "bool"}, {"name": "returnData", "type": "bytes"}], "name": "returnData", "type": "tuple[]"}], "stateMutability": "payable", "type": "function"},
]

# Buy/Sell events on hook
HOOK_EVENTS_ABI = [
    {"anonymous": False, "inputs": [
        {"indexed": True, "name": "buyer", "type": "address"},
        {"indexed": True, "name": "token", "type": "address"},
        {"indexed": False, "name": "baseCurrencyValue", "type": "uint256"},
        {"indexed": False, "name": "tokenValue", "type": "uint256"},
        {"indexed": False, "name": "tokenFee", "type": "uint256"},
    ], "name": "Buy", "type": "event"},
    {"anonymous": False, "inputs": [
        {"indexed": True, "name": "seller", "type": "address"},
        {"indexed": True, "name": "token", "type": "address"},
        {"indexed": False, "name": "baseCurrencyValue", "type": "uint256"},
        {"indexed": False, "name": "tokenValue", "type": "uint256"},
        {"indexed": False, "name": "tokenFee", "type": "uint256"},
    ], "name": "Sell", "type": "event"},
]

# Gas settings
GAS_MULTIPLIER = 1.5  # bump gas price for faster inclusion
MAX_GAS_LIMIT = 400_000
