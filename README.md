# PitchTerminal

Browser-based viewer and trading panel for [pitchwc.app](https://pitchwc.app) player &
country token markets on Base L2 — live price charts, trade history, on-chain trading,
and automatic limit orders.

## Features

- **Charts** — candlestick / line charts for all 144 player and 48 country tokens,
  multiple timeframes, own/others trade markers, avg-entry price lines.
- **Trading** — market buy/sell of player tokens through the on-chain Router
  (bonding curve via Uniswap V4 hooks).
- **Limit orders** — a server-side watcher auto-executes limit buys, take-profits
  and stop-losses when the price crosses your target.
- **My Wallet** — per-token PnL summary (realized / unrealized, break-even, fees
  paid, ownership %, holder rank) for the wallet configured in `.env`.
- **Real-time** — prices and trades stream to the UI over SSE.

## Stack

- Backend — Python 3 + Flask + web3.py
- Frontend — single-file vanilla JS + lightweight-charts v4
- Chain — Base L2 (chain ID 8453)

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the values
```

`.env` variables:

| Variable | Purpose |
|----------|---------|
| `RPC_URL` | Base RPC endpoint (public `https://mainnet.base.org` works) |
| `WALLET_ADDRESS` | Wallet highlighted in trades / My Wallet (read-only) |
| `PRIVATE_KEY` | Required only for trading and limit orders |

## Run

```bash
source venv/bin/activate
python3 server.py
# dashboard: http://localhost:5555
```

## Project structure

| File | Role |
|------|------|
| `server.py` | Flask backend — API, SSE, price/event loops, trade execution, limit-order watcher |
| `static/index.html` | Single-page frontend |
| `config.py` | Contract addresses, ABIs, env vars |
| `tokens.json` | 48 countries + 144 players (checksum addresses, roles) |
| `events_cache.json` | Local Buy/Sell event cache — gitignored, rebuilt on startup if missing |
| `limit_orders.json` | Persisted limit orders — gitignored, created on first order |

## Notes

- Limit orders fire only while the server is running — it is a local watcher;
  pitchwc has no on-chain order book.
- Country tokens are view-only; trading is for player tokens.
- No paid RPC required — gas price is bumped 1.5× to compensate for the public RPC.
