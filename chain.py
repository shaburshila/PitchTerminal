from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account
import config


def get_web3() -> Web3:
    w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def get_account(w3: Web3):
    if not config.PRIVATE_KEY:
        return None
    acct = Account.from_key(config.PRIVATE_KEY)
    return acct


def get_router(w3: Web3):
    return w3.eth.contract(
        address=Web3.to_checksum_address(config.ROUTER),
        abi=config.ROUTER_ABI,
    )


def get_token(w3: Web3, address: str):
    return w3.eth.contract(
        address=Web3.to_checksum_address(address),
        abi=config.ERC20_ABI,
    )


def get_hook(w3: Web3):
    return w3.eth.contract(
        address=Web3.to_checksum_address(config.HOOK),
        abi=config.HOOK_ABI + config.HOOK_EVENTS_ABI,
    )


def get_registry(w3: Web3):
    return w3.eth.contract(
        address=Web3.to_checksum_address(config.TOKEN_REGISTRY),
        abi=config.REGISTRY_ABI,
    )


def get_balance(w3: Web3, token_address: str, wallet: str) -> int:
    token = get_token(w3, token_address)
    return token.functions.balanceOf(Web3.to_checksum_address(wallet)).call()


def get_token_info(w3: Web3, address: str) -> dict:
    token = get_token(w3, address)
    try:
        name = token.functions.name().call()
        symbol = token.functions.symbol().call()
        supply = token.functions.totalSupply().call()
        return {"address": address, "name": name, "symbol": symbol, "totalSupply": supply}
    except Exception:
        return {"address": address, "name": "?", "symbol": "?", "totalSupply": 0}


def ensure_allowance(w3: Web3, account, token_address: str, spender: str, amount: int):
    token = get_token(w3, token_address)
    wallet = account.address
    current = token.functions.allowance(wallet, Web3.to_checksum_address(spender)).call()
    if current >= amount:
        return None

    max_uint = 2**256 - 1
    tx = token.functions.approve(
        Web3.to_checksum_address(spender), max_uint
    ).build_transaction({
        "from": wallet,
        "nonce": w3.eth.get_transaction_count(wallet),
        "gas": 60_000,
        "maxFeePerGas": int(w3.eth.gas_price * config.GAS_MULTIPLIER),
        "maxPriorityFeePerGas": w3.eth.max_priority_fee,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    return receipt


def execute_buy(w3: Web3, account, player_token: str, country_amount: int, min_player_out: int):
    router = get_router(w3)
    tx = router.functions.buy(
        Web3.to_checksum_address(player_token),
        country_amount,
        min_player_out,
    ).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": config.MAX_GAS_LIMIT,
        "maxFeePerGas": int(w3.eth.gas_price * config.GAS_MULTIPLIER),
        "maxPriorityFeePerGas": w3.eth.max_priority_fee,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash, w3.eth.wait_for_transaction_receipt(tx_hash)


def execute_sell(w3: Web3, account, player_token: str, player_amount: int, min_country_out: int):
    router = get_router(w3)
    tx = router.functions.sell(
        Web3.to_checksum_address(player_token),
        player_amount,
        min_country_out,
    ).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": config.MAX_GAS_LIMIT,
        "maxFeePerGas": int(w3.eth.gas_price * config.GAS_MULTIPLIER),
        "maxPriorityFeePerGas": w3.eth.max_priority_fee,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash, w3.eth.wait_for_transaction_receipt(tx_hash)


def discover_country_tokens(w3: Web3) -> list[dict]:
    """Try to discover all 48 country tokens from the registry."""
    registry = get_registry(w3)
    countries = []
    for i in range(48):
        try:
            addr = registry.functions.getToken(i).call()
            if addr and addr != "0x" + "0" * 40:
                info = get_token_info(w3, addr)
                info["countryId"] = i
                countries.append(info)
        except Exception:
            continue
    return countries


def fetch_trade_events(w3: Web3, token_address: str | None = None, from_block: int = 0, to_block: str = "latest") -> list:
    hook = get_hook(w3)
    buy_topic = w3.keccak(text="Buy(address,address,uint256,uint256,uint256)").hex()
    sell_topic = w3.keccak(text="Sell(address,address,uint256,uint256,uint256)").hex()

    filter_params = {
        "address": Web3.to_checksum_address(config.HOOK),
        "fromBlock": from_block,
        "toBlock": to_block,
        "topics": [[buy_topic, sell_topic]],
    }
    if token_address:
        filter_params["topics"].append(None)  # buyer/seller (any)
        padded = "0x" + token_address.lower().replace("0x", "").zfill(64)
        filter_params["topics"].append([padded])

    logs = w3.eth.get_logs(filter_params)
    events = []
    for log in logs:
        is_buy = log["topics"][0].hex() == buy_topic
        trader = "0x" + log["topics"][1].hex()[-40:]
        token = "0x" + log["topics"][2].hex()[-40:]
        data = log["data"]
        base_value = int.from_bytes(data[0:32], "big")
        token_value = int.from_bytes(data[32:64], "big")
        fee = int.from_bytes(data[64:96], "big")
        events.append({
            "type": "buy" if is_buy else "sell",
            "trader": Web3.to_checksum_address(trader),
            "token": Web3.to_checksum_address(token),
            "baseValue": base_value,
            "tokenValue": token_value,
            "fee": fee,
            "block": log["blockNumber"],
            "txHash": log["transactionHash"].hex(),
        })
    return events
