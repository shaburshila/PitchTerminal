# PitchTerminal

Browser-based viewer and trading panel for [pitchwc.app](https://pitchwc.app) player &
country token markets on Base L2 — live price charts, trade history, on-chain trading,
and automatic limit orders.

## Features

- **Charts** — candlestick / line charts for all 144 player and 48 country tokens,
  multiple timeframes, own/others trade markers, avg-entry price lines.
- **Trading** — market buy/sell of player tokens through the on-chain Router
  (bonding curve via Uniswap V4 hooks).
- **Limit orders** — a server-side watcher auto-executes limit buys and
  take-profits when the price crosses your target.
- **My Wallet** — per-token PnL summary (realized / unrealized, break-even, fees
  paid, ownership %, holder rank) for the wallet configured in `.env`.
- **Wallet profile** — portfolio-wide view of your `.env` wallet: summary, open &
  closed positions, all trades, stats, allocation, on-chain balances, and a
  portfolio-value chart. Opened from the wallet chip in the header.
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

## Usage

Open **http://localhost:5555** — browse players and countries, click any token for its
chart, trade history, holders, and (if a wallet is set) your per-token PnL.

**Trading** requires `PRIVATE_KEY` in `.env`. In the trade panel:

- **Market** — buys/sells immediately at the current price.
- **Limit** — set a target price; the order is placed and fires later automatically.

### How limit orders run

The limit-order watcher lives **inside the server process** — it polls prices every
5 seconds and executes triggered orders on-chain itself.

- ✅ **Keep `python3 server.py` running in the terminal** — that process is what watches
  and fires your orders.
- ✅ **The browser tab is optional** — it is only the UI. You can close it; orders are
  still monitored and executed by the server.
- ⏸ **If you stop the server**, orders are no longer watched. Pending orders are saved
  to `limit_orders.json` and resume being watched the next time the server starts.
- 🔌 The **Orders** tab has an *Auto-execution* toggle — a kill-switch to pause/resume
  firing without stopping the server.
- 🛡 **Catch-up guard for limit buys** — on the first check after the server starts, if a
  limit buy triggers with the price already more than 20%
  (`config.LIMIT_REVIEW_THRESHOLD_PCT`) below its target, it is *not* bought automatically:
  the server was down and the market may have changed (a rug, etc.). It moves to a
  **review** state and a popup asks you to Execute or Cancel. During live operation a
  triggered order just executes — that is the order doing its job.

### Wallet profile

The **wallet chip** in the top-right header (👤 with your address) opens the **wallet
profile** — a portfolio-wide view of the `.env` wallet that replaces the chart area:

- Summary (portfolio value, realized/unrealized PnL, ROI), open & closed positions,
  full trade history, statistics, allocation, on-chain balances, and a portfolio-value
  chart over time.
- Open orders are listed with Execute / Cancel actions.
- Click any position to jump to that token's chart; click the chip again (or any token)
  to close the profile.

The bottom **Orders** tab is scoped to the currently charted token; the profile is the
place to see every order across all tokens.

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

- Limit orders are a local watcher — pitchwc has no on-chain order book.
- Country tokens are view-only; trading is for player tokens.
- No paid RPC required — gas price is bumped 1.5× to compensate for the public RPC.
