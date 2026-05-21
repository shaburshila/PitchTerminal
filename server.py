#!/usr/bin/env python3
"""PitchWC Dashboard — web viewer for pitchwc.app player markets"""

import json
import time
import threading
from pathlib import Path
from decimal import Decimal

from flask import Flask, jsonify, send_from_directory, request, Response
from web3 import Web3

import config

app = Flask(__name__, static_folder="static")

# --- Data layer ---

WEI = 10**18
DATA_DIR = Path(__file__).parent
BASE_BLOCK_TIME = 2  # Base L2 ~2 seconds per block
HOOK_DEPLOY_BLOCK = 46_167_000  # Just before first Hook log (~2.8 days before block 46290000)
EVENTS_CACHE_FILE = DATA_DIR / "events_cache.json"

# Country bonding curve hook (country tokens priced in PITCH)
COUNTRY_HOOK = "0xcae7ebfa18755d1f35ee8e0f3356f375ed5b2aa8"


def load_tokens():
    with open(DATA_DIR / "tokens.json") as f:
        return json.load(f)


TOKEN_DATA = load_tokens()
COUNTRIES = {c["symbol"]: c for c in TOKEN_DATA["countries"]}
COUNTRIES_BY_ADDR = {c["address"].lower(): c for c in TOKEN_DATA["countries"]}
PLAYERS = TOKEN_DATA["players"]
PLAYERS_BY_ADDR = {p["address"].lower(): p for p in PLAYERS}

# Display wallet address (from WALLET_ADDRESS or PRIVATE_KEY)
DISPLAY_WALLET = ""
if config.WALLET_ADDRESS:
    try:
        DISPLAY_WALLET = Web3.to_checksum_address(config.WALLET_ADDRESS)
    except Exception:
        DISPLAY_WALLET = config.WALLET_ADDRESS
elif config.PRIVATE_KEY:
    try:
        DISPLAY_WALLET = Web3(Web3.HTTPProvider(config.RPC_URL)).eth.account.from_key(config.PRIVATE_KEY).address
    except Exception:
        pass

# Cache
cache = {
    "players": [],
    "prices": {},
    "supplies": {},
    "country_prices": {},  # country_symbol -> price in PITCH
    "events": [],
    "current_block": 0,
    "last_price_update": 0,
    "last_event_block": HOOK_DEPLOY_BLOCK,
}

# SSE subscribers
sse_clients = []


def get_w3():
    return Web3(Web3.HTTPProvider(config.RPC_URL))


# --- Events cache on disk ---

def load_events_cache():
    """Load cached events from disk."""
    if EVENTS_CACHE_FILE.exists():
        try:
            with open(EVENTS_CACHE_FILE) as f:
                data = json.load(f)
            cache["events"] = data.get("events", [])
            cache["last_event_block"] = data.get("last_block", HOOK_DEPLOY_BLOCK)
            print(f"Loaded {len(cache['events'])} cached events (up to block {cache['last_event_block']})")
        except Exception as e:
            print(f"Cache load error: {e}")


def save_events_cache():
    """Save events to disk."""
    try:
        with open(EVENTS_CACHE_FILE, "w") as f:
            json.dump({
                "events": cache["events"],
                "last_block": cache["last_event_block"],
            }, f)
    except Exception as e:
        print(f"Cache save error: {e}")


# --- Multicall3: prices + supplies + country prices in 1 call ---

def fetch_prices_and_supplies(w3):
    hook = Web3.to_checksum_address(config.HOOK)
    country_hook = Web3.to_checksum_address(COUNTRY_HOOK)
    multicall = w3.eth.contract(
        address=Web3.to_checksum_address(config.MULTICALL3),
        abi=config.MULTICALL3_ABI,
    )

    price_sel = w3.keccak(text="currentPrice(address)")[:4]
    supply_sel = bytes.fromhex("18160ddd")

    calls = []
    # Player prices + supplies
    for p in PLAYERS:
        addr = p["address"]
        addr_padded = bytes.fromhex(addr[2:].zfill(64))
        calls.append((hook, True, price_sel + addr_padded))
        calls.append((Web3.to_checksum_address(addr), True, supply_sel))

    # Country prices in PITCH (from country hook)
    country_list = TOKEN_DATA["countries"]
    for c in country_list:
        addr_padded = bytes.fromhex(c["address"][2:].zfill(64))
        calls.append((country_hook, True, price_sel + addr_padded))

    results = multicall.functions.aggregate3(calls).call()

    prices = {}
    supplies = {}
    player_count = len(PLAYERS)
    for i, p in enumerate(PLAYERS):
        addr = p["address"].lower()
        price_r = results[i * 2]
        supply_r = results[i * 2 + 1]

        if price_r[0] and len(price_r[1]) >= 32:
            raw = int.from_bytes(price_r[1][:32], "big")
            prices[addr] = float(Decimal(raw) / Decimal(WEI))

        if supply_r[0] and len(supply_r[1]) >= 32:
            raw = int.from_bytes(supply_r[1][:32], "big")
            supplies[addr] = float(Decimal(raw) / Decimal(WEI))

    # Country prices
    country_prices = {}
    base_idx = player_count * 2
    for i, c in enumerate(country_list):
        r = results[base_idx + i]
        if r[0] and len(r[1]) >= 32:
            raw = int.from_bytes(r[1][:32], "big")
            country_prices[c["symbol"]] = float(Decimal(raw) / Decimal(WEI))

    return prices, supplies, country_prices


# --- Events: incremental scan ---

def fetch_new_events(w3):
    """Scan only new blocks since last cached event."""
    current_block = w3.eth.block_number
    cache["current_block"] = current_block

    from_block = cache["last_event_block"] + 1
    if from_block >= current_block:
        return []

    hook = Web3.to_checksum_address(config.HOOK)
    buy_topic = "0x" + w3.keccak(text="Buy(address,address,uint256,uint256,uint256)").hex()
    sell_topic = "0x" + w3.keccak(text="Sell(address,address,uint256,uint256,uint256)").hex()

    new_events = []
    chunk = 2000
    for start in range(from_block, current_block + 1, chunk):
        end = min(start + chunk - 1, current_block)
        try:
            logs = w3.eth.get_logs({
                "address": hook,
                "fromBlock": start,
                "toBlock": end,
                "topics": [[buy_topic, sell_topic]],
            })
            for log in logs:
                is_buy = log["topics"][0].hex() == buy_topic[2:]
                trader = Web3.to_checksum_address("0x" + log["topics"][1].hex()[-40:])
                token = Web3.to_checksum_address("0x" + log["topics"][2].hex()[-40:])
                data = log["data"]
                val0 = int.from_bytes(data[0:32], "big")
                val1 = int.from_bytes(data[32:64], "big")
                fee = int.from_bytes(data[64:96], "big")

                if is_buy:
                    base_val, token_val = val0, val1
                else:
                    base_val, token_val = val1, val0

                new_events.append({
                    "type": "buy" if is_buy else "sell",
                    "trader": trader,
                    "token": token,
                    "baseValue": base_val,
                    "tokenValue": token_val,
                    "fee": fee,
                    "block": log["blockNumber"],
                    "tx": log["transactionHash"].hex(),
                })
            time.sleep(0.2)
        except Exception:
            time.sleep(1)

    if new_events:
        cache["events"].extend(new_events)
        cache["last_event_block"] = current_block
        save_events_cache()
        print(f"+{len(new_events)} events (total: {len(cache['events'])})")
    else:
        cache["last_event_block"] = current_block

    return new_events


# --- OHLCV candles ---

def build_candles(token_addr: str, timeframe: str = "5m") -> list:
    """Build OHLCV candles from events for a given token."""
    addr = token_addr.lower()
    current_block = cache.get("current_block", 0)
    now = time.time()

    trades = []
    for ev in cache["events"]:
        if ev["token"].lower() == addr and ev["tokenValue"] > 0:
            price = float(Decimal(ev["baseValue"]) / Decimal(ev["tokenValue"]))
            blocks_ago = current_block - ev["block"]
            ts = now - (blocks_ago * BASE_BLOCK_TIME)
            vol = float(Decimal(ev["baseValue"]) / Decimal(WEI))
            trades.append({"ts": ts, "price": price, "volume": vol, "type": ev["type"]})

    if not trades:
        return []

    tf_map = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
    interval = tf_map.get(timeframe, 300)

    candles = {}
    for t in trades:
        bucket = int(t["ts"] // interval) * interval
        if bucket not in candles:
            candles[bucket] = {"open": t["price"], "high": t["price"], "low": t["price"], "close": t["price"], "volume": 0}
        c = candles[bucket]
        c["high"] = max(c["high"], t["price"])
        c["low"] = min(c["low"], t["price"])
        c["close"] = t["price"]
        c["volume"] += t["volume"]

    sorted_buckets = sorted(candles.keys())
    if len(sorted_buckets) > 1:
        filled = {}
        start_bucket = sorted_buckets[0]
        end_bucket = int(now // interval) * interval
        prev_close = candles[start_bucket]["close"]

        bucket = start_bucket
        while bucket <= end_bucket:
            if bucket in candles:
                filled[bucket] = candles[bucket]
                prev_close = candles[bucket]["close"]
            else:
                filled[bucket] = {"open": prev_close, "high": prev_close, "low": prev_close, "close": prev_close, "volume": 0}
            bucket += interval
        candles = filled

    current_price = cache["prices"].get(addr, 0)
    if current_price > 0:
        current_bucket = int(now // interval) * interval
        if current_bucket in candles:
            c = candles[current_bucket]
            c["high"] = max(c["high"], current_price)
            c["low"] = min(c["low"], current_price)
            c["close"] = current_price
        else:
            candles[current_bucket] = {"open": current_price, "high": current_price, "low": current_price, "close": current_price, "volume": 0}

    result = []
    for ts in sorted(candles.keys()):
        c = candles[ts]
        result.append({
            "time": ts,
            "open": round(c["open"], 6),
            "high": round(c["high"], 6),
            "low": round(c["low"], 6),
            "close": round(c["close"], 6),
            "volume": round(c["volume"], 4),
        })

    return result


def build_points(token_addr: str) -> list:
    """Build individual trade points for line chart."""
    addr = token_addr.lower()
    current_block = cache.get("current_block", 0)
    now = time.time()

    points = []
    for ev in cache["events"]:
        if ev["token"].lower() == addr and ev["tokenValue"] > 0:
            price = float(Decimal(ev["baseValue"]) / Decimal(ev["tokenValue"]))
            blocks_ago = current_block - ev["block"]
            ts = now - (blocks_ago * BASE_BLOCK_TIME)
            vol = float(Decimal(ev["baseValue"]) / Decimal(WEI))
            points.append({
                "time": round(ts),
                "price": round(price, 6),
                "volume": round(vol, 4),
                "type": ev["type"],
                "trader": ev["trader"],
            })

    current_price = cache["prices"].get(addr, 0)
    if current_price > 0:
        points.append({
            "time": round(now),
            "price": round(current_price, 6),
            "volume": 0,
            "type": "spot",
            "trader": "",
        })

    return points


# --- Update loops ---

def update_prices():
    try:
        w3 = get_w3()
        if not w3.is_connected():
            return
        prices, supplies, country_prices = fetch_prices_and_supplies(w3)
        cache["prices"] = prices
        cache["supplies"] = supplies
        cache["country_prices"] = country_prices
        cache["current_block"] = w3.eth.block_number
        cache["last_price_update"] = time.time()
        rebuild_player_list()
        broadcast_sse("prices")
    except Exception as e:
        print(f"Price update error: {e}")


def update_events():
    try:
        w3 = get_w3()
        if not w3.is_connected():
            return
        new = fetch_new_events(w3)
        rebuild_player_list()
        if new:
            broadcast_sse("events")
    except Exception as e:
        print(f"Event update error: {e}")


def rebuild_player_list():
    prices = cache["prices"]
    supplies = cache["supplies"]
    country_prices = cache["country_prices"]
    events = cache["events"]

    if not prices:
        return

    now = time.time()
    current_block = cache.get("current_block", 0)

    # Build per-token trade data: counts + price history with timestamps
    trade_counts = {}
    # price_at_periods[addr] = {period_key: earliest_price_in_that_window}
    price_history = {}  # addr -> list of (ts, price)
    for ev in events:
        addr = ev["token"].lower()
        trade_counts[addr] = trade_counts.get(addr, 0) + 1
        if ev["tokenValue"] > 0:
            price = float(Decimal(ev["baseValue"]) / Decimal(ev["tokenValue"]))
            blocks_ago = current_block - ev["block"]
            ts = now - (blocks_ago * BASE_BLOCK_TIME)
            if addr not in price_history:
                price_history[addr] = []
            price_history[addr].append((ts, price))

    # Compute change% for multiple periods
    periods = {
        "all": 0,
        "1d": 86400,
        "12h": 43200,
        "6h": 21600,
        "1h": 3600,
        "15m": 900,
    }

    def calc_changes(addr_low, current_price):
        changes = {}
        hist = price_history.get(addr_low, [])
        if not hist or current_price <= 0:
            return {k: 0 for k in periods}
        for period_key, secs in periods.items():
            if secs == 0:
                ref_price = hist[0][1]  # first ever trade
            else:
                cutoff = now - secs
                # Find earliest trade within the window
                ref_price = None
                for ts, p in hist:
                    if ts >= cutoff:
                        ref_price = p
                        break
                if ref_price is None:
                    # No trade in this window — use last trade before cutoff
                    for ts, p in reversed(hist):
                        if ts < cutoff:
                            ref_price = p
                            break
                if ref_price is None:
                    ref_price = hist[0][1]
            if ref_price > 0:
                changes[period_key] = round(((current_price - ref_price) / ref_price) * 100, 1)
            else:
                changes[period_key] = 0
        return changes

    players_data = []
    for p in PLAYERS:
        addr_low = p["address"].lower()
        country = COUNTRIES.get(p["country"], {})

        price_country = round(prices.get(addr_low, 0), 4)
        supply = round(supplies.get(addr_low, 0), 2)
        c_price = country_prices.get(p["country"], 0)
        price_pitch = round(price_country * c_price, 4) if c_price > 0 else 0

        changes = calc_changes(addr_low, price_country)

        players_data.append({
            "name": p["name"],
            "symbol": p["symbol"],
            "address": p["address"],
            "country": p["country"],
            "countryName": country.get("name", "?"),
            "countryAddress": country.get("address", ""),
            "role": p.get("role", ""),
            "supply": supply,
            "priceCountry": price_country,
            "pricePitch": price_pitch,
            "countryPricePitch": round(c_price, 6),
            "trades": trade_counts.get(addr_low, 0),
            "changePct": changes,
        })

    players_data.sort(key=lambda x: -x["pricePitch"])
    cache["players"] = players_data


# --- SSE ---

def broadcast_sse(event_type):
    dead = []
    for q in sse_clients:
        try:
            q.append(event_type)
        except Exception:
            dead.append(q)
    for q in dead:
        sse_clients.remove(q)


# --- Background threads ---

def price_loop():
    while True:
        update_prices()
        time.sleep(5)


def event_loop():
    while True:
        update_events()
        time.sleep(60)


# --- Routes ---

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/players")
def api_players():
    return jsonify({
        "players": cache["players"],
        "lastUpdate": cache["last_price_update"],
        "countryPrices": cache["country_prices"],
        "walletAddress": DISPLAY_WALLET,
    })


@app.route("/api/chart/<token_addr>")
def api_chart(token_addr):
    tf = request.args.get("tf", "5m")
    candles = build_candles(token_addr, tf)
    points = build_points(token_addr)
    player = PLAYERS_BY_ADDR.get(token_addr.lower(), {})
    return jsonify({
        "player": player.get("name", "?"),
        "symbol": player.get("symbol", "?"),
        "country": player.get("country", "?"),
        "candles": candles,
        "points": points,
    })


@app.route("/api/trades/<token_addr>")
def api_trades(token_addr):
    """All trades for a specific player token + wallet positions."""
    addr = token_addr.lower()
    current_block = cache.get("current_block", 0)
    now = time.time()

    # First pass: build wallet positions
    wallets = {}  # trader_addr -> {buys, sells, position, spent, received}
    raw_trades = []
    for ev in cache["events"]:
        if ev["token"].lower() != addr:
            continue
        base_val = float(Decimal(ev["baseValue"]) / Decimal(WEI))
        token_val = float(Decimal(ev["tokenValue"]) / Decimal(WEI))
        fee = float(Decimal(ev["fee"]) / Decimal(WEI))
        blocks_ago = current_block - ev["block"]
        ts = now - (blocks_ago * BASE_BLOCK_TIME)
        price = base_val / token_val if token_val > 0 else 0

        trader = ev["trader"]
        if trader not in wallets:
            wallets[trader] = {"buys": 0, "sells": 0, "position": 0, "spent": 0, "received": 0}
        w = wallets[trader]
        if ev["type"] == "buy":
            w["buys"] += 1
            w["position"] += token_val
            w["spent"] += base_val
        else:
            w["sells"] += 1
            w["position"] -= token_val
            w["received"] += base_val

        raw_trades.append({
            "type": ev["type"],
            "trader": trader,
            "traderShort": trader[:6] + ".." + trader[-4:],
            "baseValue": round(base_val, 4),
            "tokenValue": round(token_val, 4),
            "price": round(price, 6),
            "fee": round(fee, 4),
            "tx": ev["tx"],
            "timestamp": round(ts),
        })

    # Second pass: attach current wallet position to each trade
    trades = []
    for t in raw_trades:
        w = wallets.get(t["trader"], {})
        t["walletPosition"] = round(w.get("position", 0), 2)
        t["walletBuys"] = w.get("buys", 0)
        t["walletSells"] = w.get("sells", 0)
        trades.append(t)

    # Build wallet summary sorted by position (largest first)
    wallet_list = []
    for addr_w, w in wallets.items():
        wallet_list.append({
            "address": addr_w,
            "short": addr_w[:6] + ".." + addr_w[-4:],
            "buys": w["buys"],
            "sells": w["sells"],
            "position": round(w["position"], 4),
            "spent": round(w["spent"], 4),
            "received": round(w["received"], 4),
        })
    wallet_list.sort(key=lambda x: -x["position"])

    return jsonify({
        "trades": list(reversed(trades)),
        "wallets": wallet_list,
        "totalTrades": len(trades),
    })


# --- Trading ---

def get_wallet():
    """Get wallet address from private key, or None."""
    if not config.PRIVATE_KEY:
        return None
    w3 = get_w3()
    account = w3.eth.account.from_key(config.PRIVATE_KEY)
    return account.address


@app.route("/api/wallet")
def api_wallet():
    """Get wallet info: address, balances, allowances."""
    addr = get_wallet()
    if not addr:
        return jsonify({"connected": False, "address": None})

    w3 = get_w3()
    player_addr = request.args.get("player", "").strip()

    # ETH balance
    eth_balance = float(Decimal(w3.eth.get_balance(addr)) / Decimal(WEI))

    # If player specified, get country token + player token balances and allowances
    result = {
        "connected": True,
        "address": addr,
        "addressShort": addr[:6] + ".." + addr[-4:],
        "ethBalance": round(eth_balance, 6),
    }

    if player_addr:
        player = PLAYERS_BY_ADDR.get(player_addr.lower(), {})
        if player:
            country = COUNTRIES.get(player.get("country", ""), {})
            country_addr = country.get("address", "")

            multicall = w3.eth.contract(
                address=Web3.to_checksum_address(config.MULTICALL3),
                abi=config.MULTICALL3_ABI,
            )

            bal_sel = bytes.fromhex("70a08231")  # balanceOf(address)
            allow_sel = bytes.fromhex("dd62ed3e")  # allowance(owner, spender)
            addr_padded = bytes.fromhex(addr[2:].lower().zfill(64))
            router_padded = bytes.fromhex(config.ROUTER[2:].lower().zfill(64))

            calls = []
            # 0: country token balance
            calls.append((Web3.to_checksum_address(country_addr), True, bal_sel + addr_padded))
            # 1: player token balance
            calls.append((Web3.to_checksum_address(player_addr), True, bal_sel + addr_padded))
            # 2: country token allowance for router
            calls.append((Web3.to_checksum_address(country_addr), True, allow_sel + addr_padded + router_padded))
            # 3: player token allowance for router
            calls.append((Web3.to_checksum_address(player_addr), True, allow_sel + addr_padded + router_padded))

            results = multicall.functions.aggregate3(calls).call()

            def parse_uint(r):
                if r[0] and len(r[1]) >= 32:
                    return int.from_bytes(r[1][:32], "big")
                return 0

            result["countryBalance"] = float(Decimal(parse_uint(results[0])) / Decimal(WEI))
            result["playerBalance"] = float(Decimal(parse_uint(results[1])) / Decimal(WEI))
            result["countryAllowance"] = float(Decimal(parse_uint(results[2])) / Decimal(WEI))
            result["playerAllowance"] = float(Decimal(parse_uint(results[3])) / Decimal(WEI))
            result["countrySymbol"] = player.get("country", "?")
            result["playerSymbol"] = player.get("symbol", "?")

    return jsonify(result)


@app.route("/api/quote")
def api_quote():
    """Get quote for buy or sell."""
    player_addr = request.args.get("player", "").strip()
    side = request.args.get("side", "buy")
    amount = request.args.get("amount", "0")

    try:
        amount_wei = int(Decimal(amount) * Decimal(WEI))
    except Exception:
        return jsonify({"error": "Invalid amount"}), 400

    if amount_wei <= 0:
        return jsonify({"error": "Amount must be > 0"}), 400

    w3 = get_w3()
    hook = w3.eth.contract(
        address=Web3.to_checksum_address(config.HOOK),
        abi=config.HOOK_ABI,
    )

    try:
        player_cs = Web3.to_checksum_address(player_addr)
        if side == "buy":
            out = hook.functions.quoteBuy(player_cs, amount_wei).call()
        else:
            out = hook.functions.quoteSell(player_cs, amount_wei).call()

        return jsonify({
            "side": side,
            "amountIn": amount,
            "amountOut": float(Decimal(out) / Decimal(WEI)),
            "price": float(Decimal(amount_wei) / Decimal(out)) if out > 0 else 0,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trade", methods=["POST"])
def api_trade():
    """Execute buy or sell transaction."""
    if not config.PRIVATE_KEY:
        return jsonify({"error": "No wallet configured. Add PRIVATE_KEY to .env"}), 400

    data = request.get_json()
    player_addr = data.get("player", "")
    side = data.get("side", "buy")
    amount = data.get("amount", "0")
    slippage = data.get("slippage", 50)  # basis points, default 0.5%

    try:
        amount_wei = int(Decimal(amount) * Decimal(WEI))
    except Exception:
        return jsonify({"error": "Invalid amount"}), 400

    if amount_wei <= 0:
        return jsonify({"error": "Amount must be > 0"}), 400

    w3 = get_w3()
    account = w3.eth.account.from_key(config.PRIVATE_KEY)
    player_cs = Web3.to_checksum_address(player_addr)

    hook = w3.eth.contract(
        address=Web3.to_checksum_address(config.HOOK),
        abi=config.HOOK_ABI,
    )
    router = w3.eth.contract(
        address=Web3.to_checksum_address(config.ROUTER),
        abi=config.ROUTER_ABI,
    )

    try:
        # Get quote for minOut calculation
        if side == "buy":
            quote_out = hook.functions.quoteBuy(player_cs, amount_wei).call()
        else:
            quote_out = hook.functions.quoteSell(player_cs, amount_wei).call()

        min_out = quote_out * (10000 - slippage) // 10000

        # Check allowance and approve if needed
        player_info = PLAYERS_BY_ADDR.get(player_addr.lower(), {})
        country = COUNTRIES.get(player_info.get("country", ""), {})

        if side == "buy":
            token_to_approve = Web3.to_checksum_address(country.get("address", ""))
        else:
            token_to_approve = player_cs

        erc20 = w3.eth.contract(address=token_to_approve, abi=config.ERC20_ABI)
        allowance = erc20.functions.allowance(account.address, Web3.to_checksum_address(config.ROUTER)).call()

        nonce = w3.eth.get_transaction_count(account.address)
        gas_price = int(w3.eth.gas_price * config.GAS_MULTIPLIER)

        if allowance < amount_wei:
            # Approve max
            max_uint = 2**256 - 1
            approve_tx = erc20.functions.approve(
                Web3.to_checksum_address(config.ROUTER), max_uint
            ).build_transaction({
                "from": account.address,
                "nonce": nonce,
                "gasPrice": gas_price,
                "gas": 80_000,
                "chainId": config.CHAIN_ID,
            })
            signed_approve = account.sign_transaction(approve_tx)
            approve_hash = w3.eth.send_raw_transaction(signed_approve.raw_transaction)
            w3.eth.wait_for_transaction_receipt(approve_hash, timeout=30)
            nonce += 1

        # Build trade tx
        if side == "buy":
            tx = router.functions.buy(player_cs, amount_wei, min_out)
        else:
            tx = router.functions.sell(player_cs, amount_wei, min_out)

        built_tx = tx.build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gasPrice": gas_price,
            "gas": config.MAX_GAS_LIMIT,
            "chainId": config.CHAIN_ID,
        })

        signed_tx = account.sign_transaction(built_tx)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        return jsonify({
            "success": receipt["status"] == 1,
            "tx": tx_hash.hex(),
            "gasUsed": receipt["gasUsed"],
            "amountIn": amount,
            "expectedOut": float(Decimal(quote_out) / Decimal(WEI)),
            "minOut": float(Decimal(min_out) / Decimal(WEI)),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stream")
def api_stream():
    def gen():
        q = []
        sse_clients.append(q)
        try:
            while True:
                if q:
                    event_type = q.pop(0)
                    yield f"data: {event_type}\n\n"
                else:
                    time.sleep(1)
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            if q in sse_clients:
                sse_clients.remove(q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    print("Starting PITCH Markets...")
    load_events_cache()

    # Initial price fetch (fast - 1 Multicall)
    update_prices()
    print(f"Prices loaded for {len(cache['prices'])} players")
    print(f"Country prices: {len(cache['country_prices'])} countries")

    # Incremental event scan (from cache or deployment block)
    update_events()
    print(f"Total events: {len(cache['events'])}")

    threading.Thread(target=price_loop, daemon=True).start()
    threading.Thread(target=event_loop, daemon=True).start()

    print("Dashboard: http://localhost:5555")
    app.run(host="0.0.0.0", port=5555, debug=False, threaded=True)
