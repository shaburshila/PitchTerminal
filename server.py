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
LIMIT_ORDERS_FILE = DATA_DIR / "limit_orders.json"

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
    "country_prices": {},     # country_symbol -> price in PITCH
    "country_supplies": {},   # country_symbol -> total supply
    "countries": [],          # rebuilt country list (sorted by price)
    "events": [],
    "current_block": 0,
    "last_price_update": 0,
    "last_event_block": HOOK_DEPLOY_BLOCK,
    "country_backfilled": False,  # whether Country Hook history has been scanned
}

# SSE subscribers
sse_clients = []

# Serializes mutations + writes of the events cache across threads (RLock = re-entrant)
cache_lock = threading.RLock()

# --- Limit orders ---
# pitchwc has no on-chain order book (bonding curve via Uniswap V4 hooks), so limit
# orders are a server-side watcher: price_loop checks targets and fires market trades.
orders_state = {"orders": [], "armed": True, "nextId": 1}
orders_lock = threading.RLock()   # serializes order-list mutations + writes
trade_lock = threading.Lock()     # serializes tx sending (nonce safety: manual + auto)
# Flips True after the first order check post-startup. That first check is the
# "catch-up": a limit buy triggered far below target there means the server was down
# and the market may have changed -> review. Live triggers afterwards just execute.
orders_catchup_done = False


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
            # Keep events chronological — country backfill can append older blocks
            cache["events"].sort(key=lambda e: e["block"])
            cache["last_event_block"] = data.get("last_block", HOOK_DEPLOY_BLOCK)
            cache["country_backfilled"] = data.get("country_backfilled", False)
            print(f"Loaded {len(cache['events'])} cached events (up to block {cache['last_event_block']})")
        except Exception as e:
            print(f"Cache load error: {e}")


def save_events_cache():
    """Save events to disk."""
    try:
        with cache_lock:
            with open(EVENTS_CACHE_FILE, "w") as f:
                json.dump({
                    "events": cache["events"],
                    "last_block": cache["last_event_block"],
                    "country_backfilled": cache["country_backfilled"],
                }, f)
    except Exception as e:
        print(f"Cache save error: {e}")


# --- Limit orders on disk ---

def load_orders():
    """Load limit orders from disk."""
    if not LIMIT_ORDERS_FILE.exists():
        return
    try:
        with open(LIMIT_ORDERS_FILE) as f:
            data = json.load(f)
        orders_state["orders"] = data.get("orders", [])
        orders_state["armed"] = data.get("armed", True)
        orders_state["nextId"] = data.get("nextId", 1)
        # An order left mid-execution means the server died during a tx — mark it
        # failed rather than risk re-firing (the tx may already have landed).
        for o in orders_state["orders"]:
            if o["status"] == "executing":
                o["status"] = "failed"
                o["error"] = "server restarted during execution"
        pending = sum(1 for o in orders_state["orders"] if o["status"] == "pending")
        print(f"Loaded {len(orders_state['orders'])} limit orders ({pending} pending)")
    except Exception as e:
        print(f"Orders load error: {e}")


def save_orders():
    """Persist limit orders to disk."""
    try:
        with orders_lock:
            with open(LIMIT_ORDERS_FILE, "w") as f:
                json.dump(orders_state, f, indent=2)
    except Exception as e:
        print(f"Orders save error: {e}")


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

    # Country supplies (totalSupply on each country token)
    for c in country_list:
        calls.append((Web3.to_checksum_address(c["address"]), True, supply_sel))

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

    # Country supplies
    country_supplies = {}
    supply_base = player_count * 2 + len(country_list)
    for i, c in enumerate(country_list):
        r = results[supply_base + i]
        if r[0] and len(r[1]) >= 32:
            raw = int.from_bytes(r[1][:32], "big")
            country_supplies[c["symbol"]] = float(Decimal(raw) / Decimal(WEI))

    return prices, supplies, country_prices, country_supplies


# --- Events: incremental scan ---

def _decode_log(log, buy_topic):
    """Decode a Buy/Sell hook log into a normalized event dict.

    Player Hook: token = player token, baseValue = country tokens.
    Country Hook: token = country token, baseValue = PITCH tokens.
    Data field order differs between buy and sell (see contracts.md).
    """
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

    return {
        "type": "buy" if is_buy else "sell",
        "trader": trader,
        "token": token,
        "baseValue": base_val,
        "tokenValue": token_val,
        "fee": fee,
        "block": log["blockNumber"],
        "tx": log["transactionHash"].hex(),
    }


def scan_hook_logs(w3, addresses, from_block, to_block):
    """Scan Buy/Sell logs for the given hook address(es) in [from_block, to_block]."""
    if from_block > to_block:
        return []

    buy_topic = "0x" + w3.keccak(text="Buy(address,address,uint256,uint256,uint256)").hex()
    sell_topic = "0x" + w3.keccak(text="Sell(address,address,uint256,uint256,uint256)").hex()

    events = []
    chunk = 2000
    for start in range(from_block, to_block + 1, chunk):
        end = min(start + chunk - 1, to_block)
        try:
            logs = w3.eth.get_logs({
                "address": addresses,
                "fromBlock": start,
                "toBlock": end,
                "topics": [[buy_topic, sell_topic]],
            })
            for log in logs:
                events.append(_decode_log(log, buy_topic))
            time.sleep(0.2)
        except Exception:
            time.sleep(1)

    return events


def fetch_new_events(w3):
    """Scan new blocks since last cached event for both Player + Country hooks."""
    current_block = w3.eth.block_number
    cache["current_block"] = current_block

    from_block = cache["last_event_block"] + 1
    if from_block >= current_block:
        return []

    addresses = [
        Web3.to_checksum_address(config.HOOK),
        Web3.to_checksum_address(COUNTRY_HOOK),
    ]
    new_events = scan_hook_logs(w3, addresses, from_block, current_block)

    cache["last_event_block"] = current_block
    if new_events:
        with cache_lock:
            cache["events"].extend(new_events)
            cache["events"].sort(key=lambda e: e["block"])
        save_events_cache()
        print(f"+{len(new_events)} events (total: {len(cache['events'])})")
    else:
        save_events_cache()

    return new_events


def run_country_backfill(end_block):
    """One-time historical scan of Country Hook events for caches built before
    country support existed. Atomic: events appended only on full completion."""
    try:
        w3 = get_w3()
        if not w3.is_connected():
            print("Country backfill skipped: no RPC connection")
            return
        print(f"Country backfill: scanning blocks {HOOK_DEPLOY_BLOCK}..{end_block}")
        country_events = scan_hook_logs(
            w3, [Web3.to_checksum_address(COUNTRY_HOOK)], HOOK_DEPLOY_BLOCK, end_block)
        with cache_lock:
            cache["events"].extend(country_events)
            cache["events"].sort(key=lambda e: e["block"])
            cache["country_backfilled"] = True
            save_events_cache()
        rebuild_player_list()
        broadcast_sse("events")
        print(f"Country backfill complete: +{len(country_events)} country events")
    except Exception as e:
        print(f"Country backfill error: {e}")


# --- OHLCV candles ---

def current_price_of(addr: str) -> float:
    """Current spot price for any token: player price in country tokens,
    or country price in PITCH."""
    addr = addr.lower()
    if addr in cache["prices"]:
        return cache["prices"][addr]
    c = COUNTRIES_BY_ADDR.get(addr)
    if c:
        return cache["country_prices"].get(c["symbol"], 0)
    return 0


def build_candles(token_addr: str, timeframe: str = "5m") -> list:
    """Build OHLCV candles from events for a given token."""
    addr = token_addr.lower()
    current_block = cache.get("current_block", 0)
    now = time.time()

    trades = []
    with cache_lock:  # snapshot — the event loop may sort cache["events"] concurrently
        events = list(cache["events"])
    for ev in events:
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

    current_price = current_price_of(addr)
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
    with cache_lock:  # snapshot — the event loop may sort cache["events"] concurrently
        events = list(cache["events"])
    for ev in events:
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

    current_price = current_price_of(addr)
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
        prices, supplies, country_prices, country_supplies = fetch_prices_and_supplies(w3)
        cache["prices"] = prices
        cache["supplies"] = supplies
        cache["country_prices"] = country_prices
        cache["country_supplies"] = country_supplies
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
            "kind": "player",
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

    # Build country list — priced directly in PITCH, sorted by price descending
    countries_data = []
    for c in TOKEN_DATA["countries"]:
        addr_low = c["address"].lower()
        price = country_prices.get(c["symbol"], 0)
        countries_data.append({
            "kind": "country",
            "name": c["name"],
            "symbol": c["symbol"],
            "address": c["address"],
            "supply": round(cache["country_supplies"].get(c["symbol"], 0), 2),
            "pricePitch": round(price, 6),
            "trades": trade_counts.get(addr_low, 0),
            "changePct": calc_changes(addr_low, price),
        })

    countries_data.sort(key=lambda x: -x["pricePitch"])
    cache["countries"] = countries_data


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
        try:
            check_limit_orders()
        except Exception as e:
            print(f"Order check error: {e}")
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
        "countries": cache["countries"],
        "lastUpdate": cache["last_price_update"],
        "countryPrices": cache["country_prices"],
        "walletAddress": DISPLAY_WALLET,
    })


@app.route("/api/chart/<token_addr>")
def api_chart(token_addr):
    tf = request.args.get("tf", "5m")
    candles = build_candles(token_addr, tf)
    points = build_points(token_addr)
    addr_low = token_addr.lower()
    player = PLAYERS_BY_ADDR.get(addr_low)
    country = COUNTRIES_BY_ADDR.get(addr_low)
    if player:
        kind, name, symbol, ctry = "player", player["name"], player["symbol"], player["country"]
    elif country:
        kind, name, symbol, ctry = "country", country["name"], country["symbol"], ""
    else:
        kind, name, symbol, ctry = "unknown", "?", "?", ""
    return jsonify({
        "kind": kind,
        "player": name,
        "name": name,
        "symbol": symbol,
        "country": ctry,
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
    with cache_lock:  # snapshot — the event loop may sort cache["events"] concurrently
        events = list(cache["events"])
    for ev in events:
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
            wallets[trader] = {"buys": 0, "sells": 0, "position": 0,
                               "spent": 0, "received": 0, "bought": 0,
                               "fees": 0, "firstTs": None}
        w = wallets[trader]
        if ev["type"] == "buy":
            w["buys"] += 1
            w["position"] += token_val
            w["spent"] += base_val
            w["bought"] += token_val
        else:
            w["sells"] += 1
            w["position"] -= token_val
            w["received"] += base_val
        w["fees"] += fee
        if w["firstTs"] is None:  # events are block-sorted -> first seen = earliest
            w["firstTs"] = ts

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

    # Build wallet summary sorted by position (largest first).
    # avgBuy = cost basis of all buys (spent / tokens bought).
    # avgNet = net cost of the current open position ((spent - received) / position).
    wallet_list = []
    for addr_w, w in wallets.items():
        avg_buy = (w["spent"] / w["bought"]) if w["bought"] > 0 else 0
        avg_net = ((w["spent"] - w["received"]) / w["position"]) if w["position"] > 1e-9 else 0
        wallet_list.append({
            "address": addr_w,
            "short": addr_w[:6] + ".." + addr_w[-4:],
            "buys": w["buys"],
            "sells": w["sells"],
            "position": round(w["position"], 4),
            "spent": round(w["spent"], 4),
            "received": round(w["received"], 4),
            "avgBuy": round(avg_buy, 6),
            "avgNet": round(max(avg_net, 0), 6),
        })
    wallet_list.sort(key=lambda x: -x["position"])

    # --- Configured wallet (.env) summary for THIS token ---
    # realized/unrealized split uses avg-cost basis (avgBuy = spent / bought);
    # break-even = avgNet = (spent - received) / position -> price where total PnL is 0.
    my_wallet = {"configured": bool(DISPLAY_WALLET),
                 "address": DISPLAY_WALLET or None, "hasActivity": False}
    if DISPLAY_WALLET:
        dw = DISPLAY_WALLET.lower()
        w = next((v for k, v in wallets.items() if k.lower() == dw), None)
        if w:
            cur_price = current_price_of(addr)
            supply = cache.get("supplies", {}).get(addr, 0)
            if not supply:
                cc = COUNTRIES_BY_ADDR.get(addr)
                if cc:
                    supply = cache.get("country_supplies", {}).get(cc["symbol"], 0)

            bought = w["bought"]
            position = max(w["position"], 0)
            spent, received = w["spent"], w["received"]
            sold = max(bought - position, 0)
            avg_buy = (spent / bought) if bought > 0 else 0
            break_even = ((spent - received) / position) if position > 1e-9 else 0
            realized = received - avg_buy * sold
            position_value = position * cur_price
            unrealized = position_value - avg_buy * position
            total_pnl = realized + unrealized
            rank = next((i + 1 for i, x in enumerate(wallet_list)
                         if x["address"].lower() == dw), 0)
            first_ts = w["firstTs"]

            my_wallet = {
                "configured": True,
                "address": DISPLAY_WALLET,
                "hasActivity": True,
                "buys": w["buys"],
                "sells": w["sells"],
                "position": round(position, 4),
                "positionValue": round(position_value, 4),
                "spent": round(spent, 4),
                "received": round(received, 4),
                "tokensSold": round(sold, 4),
                "avgBuy": round(avg_buy, 6),
                "breakEven": round(max(break_even, 0), 6),
                "currentPrice": round(cur_price, 6),
                "realizedPnl": round(realized, 4),
                "unrealizedPnl": round(unrealized, 4),
                "totalPnl": round(total_pnl, 4),
                "totalPnlPct": round((total_pnl / spent * 100) if spent > 0 else 0, 2),
                "breakEvenDistPct": round(((cur_price - break_even) / break_even * 100)
                                          if break_even > 1e-9 else 0, 2),
                "feesPaid": round(w["fees"], 4),
                "ownershipPct": round((position / supply * 100) if supply > 0 else 0, 4),
                "rank": rank,
                "holdersCount": len(wallet_list),
                "firstTradeTs": round(first_ts) if first_ts else None,
                "holdingDays": round((now - first_ts) / 86400, 1) if first_ts else 0,
            }

    return jsonify({
        "trades": list(reversed(trades)),
        "wallets": wallet_list,
        "totalTrades": len(trades),
        "myWallet": my_wallet,
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


def execute_trade(player_addr, side, amount, slippage=50):
    """Quote -> (approve) -> swap on the Router. Players only.

    Shared by the manual /api/trade endpoint and the limit-order watcher.
    Serialized by trade_lock so manual trades and auto-fired orders never race
    on the account nonce. Returns a dict: {success, tx, ...} or {error}.
    """
    if not config.PRIVATE_KEY:
        return {"error": "No wallet configured. Add PRIVATE_KEY to .env"}

    try:
        amount_wei = int(Decimal(str(amount)) * Decimal(WEI))
    except Exception:
        return {"error": "Invalid amount"}
    if amount_wei <= 0:
        return {"error": "Amount must be > 0"}

    # Clamp slippage to [0, 5000] bps — guards against a negative minOut
    # (slippage > 10000 would make minOut negative and the tx unencodable).
    try:
        slippage = max(0, min(int(slippage), 5000))
    except (TypeError, ValueError):
        slippage = 50

    player_info = PLAYERS_BY_ADDR.get(str(player_addr).lower())
    if not player_info:
        return {"error": "Unknown player token"}

    with trade_lock:
        try:
            w3 = get_w3()
            account = w3.eth.account.from_key(config.PRIVATE_KEY)
            player_cs = Web3.to_checksum_address(player_addr)
            hook = w3.eth.contract(address=Web3.to_checksum_address(config.HOOK),
                                   abi=config.HOOK_ABI)
            router = w3.eth.contract(address=Web3.to_checksum_address(config.ROUTER),
                                     abi=config.ROUTER_ABI)
            country = COUNTRIES.get(player_info.get("country", ""), {})

            # Token paid in: country token when buying, player token when selling
            if side == "buy":
                pay_token = Web3.to_checksum_address(country.get("address", ""))
            else:
                pay_token = player_cs
            erc20 = w3.eth.contract(address=pay_token, abi=config.ERC20_ABI)

            # Balance pre-check — fail fast instead of paying gas for a revert
            balance = erc20.functions.balanceOf(account.address).call()
            if balance < amount_wei:
                have = float(Decimal(balance) / Decimal(WEI))
                return {"error": f"Insufficient balance: have {have:.4f}, need {amount}"}

            # Quote for minOut
            if side == "buy":
                quote_out = hook.functions.quoteBuy(player_cs, amount_wei).call()
            else:
                quote_out = hook.functions.quoteSell(player_cs, amount_wei).call()
            min_out = quote_out * (10000 - slippage) // 10000

            allowance = erc20.functions.allowance(
                account.address, Web3.to_checksum_address(config.ROUTER)).call()
            # 'pending' count so a tx already in the mempool isn't reused
            nonce = w3.eth.get_transaction_count(account.address, "pending")
            gas_price = int(w3.eth.gas_price * config.GAS_MULTIPLIER)

            if allowance < amount_wei:
                approve_tx = erc20.functions.approve(
                    Web3.to_checksum_address(config.ROUTER), 2**256 - 1
                ).build_transaction({
                    "from": account.address, "nonce": nonce,
                    "gasPrice": gas_price, "gas": 80_000, "chainId": config.CHAIN_ID,
                })
                signed_approve = account.sign_transaction(approve_tx)
                approve_hash = w3.eth.send_raw_transaction(signed_approve.raw_transaction)
                w3.eth.wait_for_transaction_receipt(approve_hash, timeout=30)
                nonce += 1

            if side == "buy":
                tx = router.functions.buy(player_cs, amount_wei, min_out)
            else:
                tx = router.functions.sell(player_cs, amount_wei, min_out)
            built_tx = tx.build_transaction({
                "from": account.address, "nonce": nonce,
                "gasPrice": gas_price, "gas": config.MAX_GAS_LIMIT,
                "chainId": config.CHAIN_ID,
            })
            signed_tx = account.sign_transaction(built_tx)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            return {
                "success": receipt["status"] == 1,
                "tx": tx_hash.hex(),
                "gasUsed": receipt["gasUsed"],
                "amountIn": str(amount),
                "expectedOut": float(Decimal(quote_out) / Decimal(WEI)),
                "minOut": float(Decimal(min_out) / Decimal(WEI)),
            }
        except Exception as e:
            return {"error": str(e)}


@app.route("/api/trade", methods=["POST"])
def api_trade():
    """Execute a market buy/sell immediately."""
    data = request.get_json() or {}
    result = execute_trade(data.get("player", ""), data.get("side", "buy"),
                           data.get("amount", "0"), data.get("slippage", 50))
    return jsonify(result), (200 if result.get("success") else 400)


# --- Limit orders: watcher + API ---

def _order_kind(side):
    """Display kind: limit buy (buy) or take-profit (sell)."""
    return "limit" if side == "buy" else "tp"


def run_order(order_id):
    """Execute a triggered limit order in its own thread, then record the result."""
    with orders_lock:
        o = next((x for x in orders_state["orders"] if x["id"] == order_id), None)
        if not o or o["status"] != "executing":
            return
        player, side = o["player"], o["side"]
        amount, slippage = o["amount"], o["slippage"]

    result = execute_trade(player, side, amount, slippage)

    with orders_lock:
        o = next((x for x in orders_state["orders"] if x["id"] == order_id), None)
        if o:
            if result.get("success"):
                o["status"] = "filled"
                o["tx"] = result.get("tx")
                # Fill price in country units from the executed quote (amount in /
                # tokens out), not post-trade spot — the trade itself moved the curve.
                amt = float(result.get("amountIn", 0) or 0)
                out = float(result.get("expectedOut", 0) or 0)
                fill_price = (amt / out) if (side == "buy" and out > 0) \
                    else ((out / amt) if (side == "sell" and amt > 0) else 0)
                o["filledPrice"] = round(fill_price, 6)
                o["filledAt"] = round(time.time())
            else:
                o["status"] = "failed"
                o["error"] = result.get("error", "transaction reverted")
            save_orders()
    print(f"Order #{order_id}: {'FILLED' if result.get('success') else 'FAILED'}")
    broadcast_sse("orders")


def check_limit_orders():
    """Evaluate pending limit orders against fresh prices — called every price tick."""
    global orders_catchup_done
    if not orders_state.get("armed", True):
        return
    if not cache.get("prices"):
        return  # prices not loaded yet — wait so the catch-up check sees real data
    # First armed check after startup = the catch-up: re-evaluates a possible downtime
    # gap. Only there does the review guard apply. Live triggers afterwards execute.
    catchup = not orders_catchup_done
    orders_catchup_done = True
    triggered, reviewed = [], []
    with orders_lock:
        for o in orders_state["orders"]:
            if o["status"] != "pending":
                continue
            price = current_price_of(o["player"])
            if price <= 0:
                continue
            hit = (price <= o["targetPrice"]) if o["trigger"] == "below" \
                else (price >= o["targetPrice"])
            if not hit:
                continue
            # On the post-startup catch-up only: a limit buy triggered far below target
            # means the server was down and the market may have changed — hold it for a
            # manual decision instead of buying blindly. During live operation the
            # order just executes (it triggered, that's its job).
            if catchup and o["side"] == "buy":
                floor = o["targetPrice"] * (1 - config.LIMIT_REVIEW_THRESHOLD_PCT / 100)
                if price < floor:
                    o["status"] = "review"
                    o["reviewPrice"] = round(price, 6)
                    o["reviewAt"] = round(time.time())
                    reviewed.append(o["id"])
                    continue
            o["status"] = "executing"
            triggered.append(o["id"])
        if triggered or reviewed:
            save_orders()
    for oid in triggered:
        threading.Thread(target=run_order, args=(oid,), daemon=True).start()
    for oid in reviewed:
        print(f"Order #{oid}: HELD FOR REVIEW (price far below target)")
    if triggered or reviewed:
        broadcast_sse("orders")


@app.route("/api/orders", methods=["GET"])
def api_orders_list():
    """All limit orders, newest first."""
    with orders_lock:
        return jsonify({
            "orders": list(reversed(orders_state["orders"])),
            "armed": orders_state.get("armed", True),
        })


@app.route("/api/orders", methods=["POST"])
def api_orders_create():
    """Create a limit order. Body: {player, side, targetPrice, amount, slippage}.
    targetPrice is in country tokens (the player's native price)."""
    if not config.PRIVATE_KEY:
        return jsonify({"error": "No wallet configured. Add PRIVATE_KEY to .env"}), 400

    data = request.get_json() or {}
    player_addr = str(data.get("player", "")).lower()
    side = data.get("side", "buy")
    player_info = PLAYERS_BY_ADDR.get(player_addr)
    if not player_info:
        return jsonify({"error": "Limit orders are for player tokens only"}), 400
    if side not in ("buy", "sell"):
        return jsonify({"error": "Invalid side"}), 400
    try:
        target = float(data.get("targetPrice", 0))
        amount = float(data.get("amount", 0))
    except Exception:
        return jsonify({"error": "Invalid number"}), 400
    if target <= 0 or amount <= 0:
        return jsonify({"error": "Target price and amount must be > 0"}), 400
    slippage = int(data.get("slippage", 50))

    spot = current_price_of(player_addr)
    # Buy = limit buy (watch for the price falling to target). Sell = take-profit
    # only — it must trigger above the current price; stop-loss is not supported.
    if side == "buy":
        trigger = "below"
    else:
        if target <= spot:
            return jsonify({"error": "Sell (take-profit) target must be "
                                     "above the current price"}), 400
        trigger = "above"

    with orders_lock:
        order = {
            "id": orders_state["nextId"],
            "player": player_addr,
            "playerSymbol": player_info.get("symbol", "?"),
            "country": player_info.get("country", "?"),
            "side": side,
            "trigger": trigger,
            "kind": _order_kind(side),
            "targetPrice": round(target, 6),
            "amount": amount,
            "slippage": slippage,
            "spotAtCreate": round(spot, 6),
            "status": "pending",
            "createdAt": round(time.time()),
            "tx": None, "filledPrice": None, "filledAt": None, "error": None,
            "reviewPrice": None, "reviewAt": None,
        }
        orders_state["orders"].append(order)
        orders_state["nextId"] += 1
        save_orders()
    broadcast_sse("orders")
    return jsonify({"order": order})


@app.route("/api/orders/<int:order_id>", methods=["DELETE"])
def api_orders_cancel(order_id):
    """Cancel a pending or under-review order."""
    with orders_lock:
        o = next((x for x in orders_state["orders"] if x["id"] == order_id), None)
        if not o:
            return jsonify({"error": "Order not found"}), 404
        if o["status"] not in ("pending", "review"):
            return jsonify({"error": f"Cannot cancel a {o['status']} order"}), 400
        o["status"] = "cancelled"
        save_orders()
    broadcast_sse("orders")
    return jsonify({"ok": True})


@app.route("/api/orders/<int:order_id>/execute", methods=["POST"])
def api_orders_execute(order_id):
    """Manually execute an order that was held for review."""
    with orders_lock:
        o = next((x for x in orders_state["orders"] if x["id"] == order_id), None)
        if not o:
            return jsonify({"error": "Order not found"}), 404
        if o["status"] != "review":
            return jsonify({"error": f"Order is {o['status']}, not awaiting review"}), 400
        o["status"] = "executing"
        save_orders()
    broadcast_sse("orders")
    threading.Thread(target=run_order, args=(order_id,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/orders/arm", methods=["POST"])
def api_orders_arm():
    """Global kill-switch for auto-execution. Body: {armed: bool}."""
    data = request.get_json() or {}
    with orders_lock:
        orders_state["armed"] = bool(data.get("armed", True))
        save_orders()
    broadcast_sse("orders")
    return jsonify({"armed": orders_state["armed"]})


# --- Wallet profile (portfolio-wide view of the .env wallet) ---

def _token_meta(addr, players_by_addr, countries_by_addr):
    """Per-token display + pricing metadata. `rate` converts the token's base
    currency to PITCH (country->PITCH for players; 1 for country tokens)."""
    p = players_by_addr.get(addr)
    if p:
        return {"kind": "player", "symbol": p["symbol"], "country": p.get("country", ""),
                "role": p.get("role", ""), "basePrice": p.get("priceCountry", 0),
                "rate": p.get("countryPricePitch", 0), "pricePitch": p.get("pricePitch", 0)}
    c = countries_by_addr.get(addr)
    if c:
        return {"kind": "country", "symbol": c["symbol"], "country": c["symbol"],
                "role": "country", "basePrice": c.get("pricePitch", 0),
                "rate": 1.0, "pricePitch": c.get("pricePitch", 0)}
    return None


def _wallet_balances(addr):
    """ETH + PITCH + country-token balances via Multicall3."""
    try:
        w3 = get_w3()
        eth = float(Decimal(w3.eth.get_balance(Web3.to_checksum_address(addr))) / Decimal(WEI))
        multicall = w3.eth.contract(address=Web3.to_checksum_address(config.MULTICALL3),
                                    abi=config.MULTICALL3_ABI)
        bal_sel = bytes.fromhex("70a08231")
        addr_padded = bytes.fromhex(addr[2:].lower().zfill(64))
        countries = TOKEN_DATA["countries"]
        targets = [config.PITCH_TOKEN] + [c["address"] for c in countries]
        calls = [(Web3.to_checksum_address(t), True, bal_sel + addr_padded) for t in targets]
        results = multicall.functions.aggregate3(calls).call()

        def parse(r):
            return (int.from_bytes(r[1][:32], "big") if r[0] and len(r[1]) >= 32 else 0)

        pitch = float(Decimal(parse(results[0])) / Decimal(WEI))
        country_bals = []
        for c, r in zip(countries, results[1:]):
            bal = float(Decimal(parse(r)) / Decimal(WEI))
            if bal >= 1e-4:
                country_bals.append({"symbol": c["symbol"], "balance": round(bal, 4)})
        country_bals.sort(key=lambda x: -x["balance"])
        return {"eth": round(eth, 6), "pitch": round(pitch, 4), "countries": country_bals}
    except Exception as e:
        print(f"Profile balances error: {e}")
        return {"eth": 0, "pitch": 0, "countries": [], "error": "balance fetch failed"}


@app.route("/api/profile")
def api_profile():
    """Portfolio-wide profile for the configured (.env) wallet."""
    if not DISPLAY_WALLET:
        return jsonify({"configured": False})
    w = DISPLAY_WALLET.lower()
    now = time.time()
    cur_block = cache.get("current_block", 0)
    players_by_addr = {p["address"].lower(): p for p in cache.get("players", [])}
    countries_by_addr = {c["address"].lower(): c for c in cache.get("countries", [])}

    # --- aggregate the wallet's events per token + collect all its trades ---
    agg = {}
    wallet_trades = []
    price_tl = {}  # token -> ([ts...], [basePrice...]) from ALL events, for the value chart
    with cache_lock:  # snapshot — the event loop may sort cache["events"] concurrently
        events = list(cache["events"])
    for ev in events:
        base = float(Decimal(ev["baseValue"]) / Decimal(WEI))
        tv = float(Decimal(ev["tokenValue"]) / Decimal(WEI))
        token = ev["token"].lower()
        ts = now - (cur_block - ev["block"]) * BASE_BLOCK_TIME
        if tv > 0:
            tl = price_tl.setdefault(token, ([], []))
            tl[0].append(ts)
            tl[1].append(base / tv)
        if ev["trader"].lower() != w:
            continue
        fee = float(Decimal(ev["fee"]) / Decimal(WEI))
        a = agg.setdefault(token, {"buys": 0, "sells": 0, "position": 0.0, "spent": 0.0,
                                   "received": 0.0, "bought": 0.0, "fees": 0.0,
                                   "firstTs": ts, "lastTs": ts})
        a["fees"] += fee
        a["lastTs"] = ts
        if ev["type"] == "buy":
            a["buys"] += 1
            a["position"] += tv
            a["spent"] += base
            a["bought"] += tv
        else:
            a["sells"] += 1
            a["position"] -= tv
            a["received"] += base
        wallet_trades.append({"token": token, "type": ev["type"], "base": base,
                              "tv": tv, "ts": ts, "tx": ev["tx"]})

    # --- per-token positions / closed / totals ---
    positions, closed = [], []
    realized_pitch = unrealized_pitch = value_pitch = spent_pitch = fees_pitch = 0.0
    volume_pitch = 0.0
    closed_count = closed_wins = 0
    pnl_by_token = {}  # symbol -> total PnL in PITCH (best/worst)
    alloc_country, alloc_role = {}, {}
    alloc_players = alloc_countries = 0.0

    for token, a in agg.items():
        meta = _token_meta(token, players_by_addr, countries_by_addr)
        if not meta:
            continue
        rate = meta["rate"]
        bought, spent, received = a["bought"], a["spent"], a["received"]
        position = max(a["position"], 0.0)
        sold = max(bought - position, 0.0)
        avg_buy = (spent / bought) if bought > 0 else 0
        realized = received - avg_buy * sold
        unreal = position * meta["basePrice"] - avg_buy * position
        realized_pitch += realized * rate
        fees_pitch += a["fees"] * rate
        spent_pitch += spent * rate
        pnl_by_token[meta["symbol"]] = (realized + unreal) * rate
        is_open = position > 1e-9

        if is_open:
            pv = position * meta["pricePitch"]
            value_pitch += pv
            unrealized_pitch += unreal * rate
            cost = avg_buy * position
            positions.append({
                "token": token, "symbol": meta["symbol"], "kind": meta["kind"],
                "country": meta["country"], "role": meta["role"],
                "qty": round(position, 4), "avgBuy": round(avg_buy, 6),
                "currentPrice": round(meta["basePrice"], 6),
                "valuePitch": round(pv, 2),
                "unrealizedPnlPitch": round(unreal * rate, 2),
                "unrealizedPct": round((unreal / cost * 100) if cost > 0 else 0, 1),
            })
            alloc_country[meta["country"]] = alloc_country.get(meta["country"], 0) + pv
            alloc_role[meta["role"]] = alloc_role.get(meta["role"], 0) + pv
            if meta["kind"] == "player":
                alloc_players += pv
            else:
                alloc_countries += pv
        elif bought > 0:
            closed_count += 1
            if realized > 0:
                closed_wins += 1
            closed.append({
                "token": token, "symbol": meta["symbol"], "kind": meta["kind"],
                "country": meta["country"],
                "realizedPnlPitch": round(realized * rate, 2),
                "buys": a["buys"], "sells": a["sells"],
                "lastTs": round(a["lastTs"]),
            })

    # --- build the trade list (newest first) ---
    trades_out = []
    for tr in reversed(wallet_trades):
        meta = _token_meta(tr["token"], players_by_addr, countries_by_addr)
        if not meta:
            continue
        vp = tr["base"] * meta["rate"]
        volume_pitch += vp
        trades_out.append({
            "symbol": meta["symbol"], "kind": meta["kind"], "type": tr["type"],
            "price": round(tr["base"] / tr["tv"], 6) if tr["tv"] > 0 else 0,
            "amount": round(tr["tv"], 4), "valuePitch": round(vp, 2),
            "timestamp": round(tr["ts"]), "tx": tr["tx"],
        })

    positions.sort(key=lambda x: -x["valuePitch"])
    closed.sort(key=lambda x: -x["lastTs"])
    for p in positions:
        p["sharePct"] = round((p["valuePitch"] / value_pitch * 100) if value_pitch > 0 else 0, 1)

    best = max(pnl_by_token.items(), key=lambda kv: kv[1], default=None)
    worst = min(pnl_by_token.items(), key=lambda kv: kv[1], default=None)
    total_pnl = realized_pitch + unrealized_pitch
    buys = sum(a["buys"] for a in agg.values())
    sells = sum(a["sells"] for a in agg.values())

    # --- portfolio value over time (sampled at each of the wallet's trades) ---
    def price_at(token, ts):
        tl = price_tl.get(token)
        if not tl or not tl[0]:
            return 0
        times = tl[0]
        lo, hi = 0, len(times)
        while lo < hi:
            mid = (lo + hi) // 2
            if times[mid] <= ts:
                lo = mid + 1
            else:
                hi = mid
        return tl[1][lo - 1] if lo > 0 else tl[1][0]

    holdings = {}
    series_map = {}
    for tr in wallet_trades:  # block-sorted -> ascending time
        holdings[tr["token"]] = holdings.get(tr["token"], 0.0) + (
            tr["tv"] if tr["type"] == "buy" else -tr["tv"])
        val = 0.0
        for tok, qty in holdings.items():
            if qty <= 1e-9:
                continue
            meta = _token_meta(tok, players_by_addr, countries_by_addr)
            if not meta:
                continue
            val += qty * price_at(tok, tr["ts"]) * meta["rate"]
        series_map[round(tr["ts"])] = round(val, 2)
    series_map[round(now)] = round(value_pitch, 2)
    value_series = [{"time": t, "value": v} for t, v in sorted(series_map.items())]

    return jsonify({
        "configured": True,
        "address": DISPLAY_WALLET,
        "summary": {
            "totalValuePitch": round(value_pitch, 2),
            "realizedPnlPitch": round(realized_pitch, 2),
            "unrealizedPnlPitch": round(unrealized_pitch, 2),
            "totalPnlPitch": round(total_pnl, 2),
            "roiPct": round((total_pnl / spent_pitch * 100) if spent_pitch > 0 else 0, 1),
            "openPositions": len(positions),
            "feesPaidPitch": round(fees_pitch, 2),
        },
        "positions": positions,
        "closed": closed,
        "trades": trades_out,
        "stats": {
            "totalTrades": len(wallet_trades), "buys": buys, "sells": sells,
            "volumePitch": round(volume_pitch, 2),
            "avgTradePitch": round(volume_pitch / len(wallet_trades), 2) if wallet_trades else 0,
            "feesPaidPitch": round(fees_pitch, 2),
            "closedPositions": closed_count,
            "winRatePct": round((closed_wins / closed_count * 100) if closed_count else 0, 1),
            "best": {"symbol": best[0], "pnlPitch": round(best[1], 2)} if best else None,
            "worst": {"symbol": worst[0], "pnlPitch": round(worst[1], 2)} if worst else None,
        },
        "allocation": {
            "byCountry": {k: round(v, 2) for k, v in sorted(
                alloc_country.items(), key=lambda kv: -kv[1])},
            "byRole": {k: round(v, 2) for k, v in alloc_role.items()},
            "players": round(alloc_players, 2),
            "countries": round(alloc_countries, 2),
        },
        "balances": _wallet_balances(w),
        "valueSeries": value_series,
    })


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
    had_cache = EVENTS_CACHE_FILE.exists()
    load_events_cache()
    load_orders()
    # Block range that predates country support — needs a one-time country backfill.
    backfill_end = cache["last_event_block"]

    # Initial price fetch (fast - 1 Multicall)
    update_prices()
    print(f"Prices loaded for {len(cache['prices'])} players")
    print(f"Country prices: {len(cache['country_prices'])} countries")

    # Incremental event scan — covers new blocks for both Player + Country hooks
    update_events()
    print(f"Total events: {len(cache['events'])}")

    # One-time historical scan of Country Hook for pre-existing caches.
    # Fresh caches are already fully covered by update_events above.
    if not cache["country_backfilled"]:
        if had_cache and backfill_end > HOOK_DEPLOY_BLOCK:
            threading.Thread(target=run_country_backfill, args=(backfill_end,),
                             daemon=True).start()
        else:
            cache["country_backfilled"] = True
            save_events_cache()

    threading.Thread(target=price_loop, daemon=True).start()
    threading.Thread(target=event_loop, daemon=True).start()

    print("Dashboard: http://localhost:5555")
    app.run(host="0.0.0.0", port=5555, debug=False, threaded=True)
