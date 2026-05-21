#!/usr/bin/env python3
"""PitchWC Terminal — DEX Screener-style trading terminal for pitchwc.app"""

import json
import time as _time
from pathlib import Path
from decimal import Decimal

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Header, Footer, Static, DataTable, Input, Button, Label,
    TabbedContent, TabPane, RichLog,
)
from textual.binding import Binding
from textual.reactive import reactive
from textual import work

from web3 import Web3
import config
import chain


WEI = 10**18


def fmt(value: int, decimals: int = 18, precision: int = 4) -> str:
    d = Decimal(value) / Decimal(10**decimals)
    return f"{d:.{precision}f}"


class TokenData:
    def __init__(self):
        data_path = Path(__file__).parent / "tokens.json"
        with open(data_path) as f:
            data = json.load(f)
        self.countries = data["countries"]
        self.players = data["players"]
        self.country_by_symbol = {c["symbol"]: c for c in self.countries}
        self.country_by_addr = {c["address"].lower(): c for c in self.countries}
        self.player_by_symbol = {p["symbol"]: p for p in self.players}
        self.player_by_addr = {p["address"].lower(): p for p in self.players}

    def get_country_for_player(self, player_symbol: str) -> dict | None:
        p = self.player_by_symbol.get(player_symbol)
        if p:
            return self.country_by_symbol.get(p["country"])
        return None

    def resolve_player(self, addr: str) -> str:
        """Return player symbol or shortened address."""
        p = self.player_by_addr.get(addr.lower())
        return p["symbol"] if p else addr[:8]

    def discover_token(self, w3: Web3, addr: str) -> None:
        """Try to discover an unknown player token and add it to data."""
        addr_lower = addr.lower()
        if addr_lower in self.player_by_addr:
            return
        try:
            info = chain.get_token_info(w3, addr)
            entry = {
                "symbol": info["symbol"],
                "name": info["name"],
                "address": Web3.to_checksum_address(addr),
                "country": "?",
            }
            self.players.append(entry)
            self.player_by_symbol[info["symbol"]] = entry
            self.player_by_addr[addr_lower] = entry
        except Exception:
            pass


class PriceChart(Static):
    """ASCII price chart for a token."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._prices: list[float] = []
        self._token_name = ""

    def set_data(self, prices: list[float], token_name: str = "") -> None:
        self._prices = prices
        self._token_name = token_name
        self.refresh()

    def render(self) -> str:
        if not self._prices:
            return "  No trade data. Press [b]R[/b] to refresh or wait for trades."

        prices = self._prices
        if len(prices) < 2:
            return f"  {self._token_name} | Price: {prices[0]:.4f} (1 data point)"

        height = 12
        width = min(len(prices), 60)

        if len(prices) > width:
            step = len(prices) / width
            sampled = [prices[int(i * step)] for i in range(width)]
        else:
            sampled = prices

        mn, mx = min(sampled), max(sampled)
        if mn == mx:
            mn -= 0.001
            mx += 0.001
        rng = mx - mn

        last = sampled[-1]
        first = sampled[0]
        change = ((last - first) / first * 100) if first else 0
        arrow = "▲" if change >= 0 else "▼"
        color = "green" if change >= 0 else "red"

        lines = []
        lines.append(f"  {self._token_name} │ Last: {last:.4f}  H: {mx:.4f}  L: {mn:.4f}  {arrow} {change:+.1f}%")
        lines.append(f"  {'─' * (width + 12)}")

        for row in range(height, -1, -1):
            threshold = mn + (rng * row / height)
            label = f"{threshold:9.4f} │"
            chars = []
            for val in sampled:
                level = int((val - mn) / rng * height)
                if level == row:
                    chars.append("█")
                elif level > row:
                    chars.append("│")
                else:
                    chars.append(" ")
            lines.append(f"{label}{''.join(chars)}")

        lines.append(f"  {'─' * (width + 12)}")
        lines.append(f"  trades: {len(prices)}  │  oldest ←  → newest")
        return "\n".join(lines)


class TradePanel(Static):
    def compose(self) -> ComposeResult:
        yield Label("═══ TRADE ═══", classes="panel-title")
        yield Horizontal(
            Label("Token:", classes="field-label"),
            Input(placeholder="e.g. DEBRUYNE", id="trade-token"),
        )
        yield Horizontal(
            Label("Amount:", classes="field-label"),
            Input(placeholder="Amount to spend/sell", id="trade-amount"),
        )
        yield Horizontal(
            Label("Slip %:", classes="field-label"),
            Input(placeholder="5", id="trade-slippage", value="5"),
        )
        yield Horizontal(
            Button("BUY", id="btn-buy", variant="success"),
            Button("SELL", id="btn-sell", variant="error"),
            Button("MAX", id="btn-max", variant="warning"),
            classes="trade-buttons",
        )
        yield RichLog(id="trade-log", max_lines=100)


class PitchTerminal(App):
    CSS = """
    Screen {
        layout: grid;
        grid-size: 2 2;
        grid-columns: 3fr 2fr;
        grid-rows: 2fr 3fr;
    }
    #chart-pane { column-span: 2; border: solid $primary; }
    #left-pane { border: solid $primary; }
    #right-pane { border: solid $primary; }
    .panel-title { text-style: bold; color: $accent; text-align: center; padding: 0 1; }
    .field-label { width: 9; padding: 0 1; }
    .trade-buttons { height: 3; padding: 0 1; }
    .trade-buttons Button { margin: 0 1; min-width: 8; }
    #trade-log { border: solid $surface; margin: 0 1; }
    DataTable { height: 100%; }
    PriceChart { height: 100%; padding: 0; }
    TradePanel { height: 100%; }
    TabbedContent { height: 100%; }
    TabPane { height: 100%; padding: 0; }
    """

    TITLE = "PITCH Terminal"
    SUB_TITLE = "pitchwc.app"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("b", "focus_buy", "Buy"),
        Binding("s", "focus_sell", "Sell"),
        Binding("1", "select_tab_portfolio", "Portfolio", show=False),
        Binding("2", "select_tab_trades", "Trades", show=False),
    ]

    selected_token: reactive[str] = reactive("DEBRUYNE")

    def __init__(self):
        super().__init__()
        self.token_data = TokenData()
        self.w3: Web3 | None = None
        self.account = None
        self.trade_history: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(PriceChart(id="price-chart"), id="chart-pane")
        with Vertical(id="left-pane"):
            with TabbedContent():
                with TabPane("Markets", id="tab-markets"):
                    yield DataTable(id="markets-table")
                with TabPane("Portfolio", id="tab-portfolio"):
                    yield DataTable(id="portfolio-table")
                with TabPane("Trades", id="tab-trades"):
                    yield DataTable(id="trades-table")
        yield Vertical(TradePanel(), id="right-pane")
        yield Footer()

    def on_mount(self) -> None:
        # Setup markets table
        mtable = self.query_one("#markets-table", DataTable)
        mtable.add_columns("Player", "Symbol", "Country", "Supply")
        mtable.cursor_type = "row"
        # Setup portfolio table
        ptable = self.query_one("#portfolio-table", DataTable)
        ptable.add_columns("Token", "Symbol", "Balance", "Type")
        ptable.cursor_type = "row"
        # Setup trades table
        ttable = self.query_one("#trades-table", DataTable)
        ttable.add_columns("Side", "Player", "Paid", "Got", "Fee", "Trader", "Block")
        ttable.cursor_type = "row"

        self.init_chain()

    @work(thread=True)
    def init_chain(self) -> None:
        try:
            self.w3 = chain.get_web3()
            self.account = chain.get_account(self.w3)
            wallet = self.account.address if self.account else "No private key in .env"
            block = self.w3.eth.block_number
            self._log(f"✓ Connected | Block: {block}")
            self._log(f"  Wallet: {wallet}")
            if not self.account:
                self._log("  ⚠ Add PRIVATE_KEY to .env to trade")
        except Exception as e:
            self._log(f"✗ Connection error: {e}")
            return

        self._refresh_data()

    def _log(self, msg: str) -> None:
        try:
            log_w = self.query_one("#trade-log", RichLog)
            self.call_from_thread(log_w.write, msg)
        except Exception:
            pass

    def _refresh_data(self) -> None:
        """Refresh all data (call from thread)."""
        self._load_markets()
        self._load_portfolio()
        self._load_trades()

    def _load_markets(self) -> None:
        """Load all known player tokens into Markets tab."""
        table = self.query_one("#markets-table", DataTable)
        self.call_from_thread(table.clear)

        # Get total supply for each player (shows market activity)
        for p in sorted(self.token_data.players, key=lambda x: x["country"]):
            supply_str = "—"
            if self.w3:
                try:
                    token = chain.get_token(self.w3, p["address"])
                    supply = token.functions.totalSupply().call()
                    supply_str = fmt(supply, precision=1) if supply > 0 else "0"
                    _time.sleep(0.1)
                except Exception:
                    pass

            self.call_from_thread(
                table.add_row,
                p["name"],
                p["symbol"],
                p["country"],
                supply_str,
            )

    def _load_portfolio(self) -> None:
        if not self.w3 or not self.account:
            return
        table = self.query_one("#portfolio-table", DataTable)
        self.call_from_thread(table.clear)
        wallet = self.account.address

        def add_row(*args):
            self.call_from_thread(table.add_row, *args)

        # ETH balance
        try:
            eth_bal = self.w3.eth.get_balance(wallet)
            if eth_bal > 0:
                add_row("ETH", "ETH", fmt(eth_bal, precision=6), "Gas")
        except Exception:
            pass

        # PITCH balance
        try:
            bal = chain.get_balance(self.w3, config.PITCH_TOKEN, wallet)
            if bal > 0:
                add_row("PITCH", "PITCH", fmt(bal), "Base")
        except Exception:
            pass
        _time.sleep(0.2)

        # Country tokens (only show non-zero)
        for c in self.token_data.countries:
            try:
                bal = chain.get_balance(self.w3, c["address"], wallet)
                if bal > 0:
                    add_row(c["name"], c["symbol"], fmt(bal), "Country")
            except Exception:
                pass
            _time.sleep(0.1)

        # Player tokens
        for p in self.token_data.players:
            try:
                bal = chain.get_balance(self.w3, p["address"], wallet)
                if bal > 0:
                    add_row(p["name"], p["symbol"], fmt(bal), "Player")
            except Exception:
                pass
            _time.sleep(0.1)

    def _load_trades(self) -> None:
        if not self.w3:
            return
        try:
            current = self.w3.eth.block_number
            events = chain.fetch_trade_events(self.w3, from_block=current - 1000)
            self.trade_history = events

            # Discover unknown tokens
            for ev in events:
                self.token_data.discover_token(self.w3, ev["token"])
                _time.sleep(0.1)

            table = self.query_one("#trades-table", DataTable)
            self.call_from_thread(table.clear)

            for ev in reversed(events[-50:]):
                p_sym = self.token_data.resolve_player(ev["token"])
                trader = ev["trader"][:6] + ".." + ev["trader"][-4:]
                side = ev["type"].upper()
                self.call_from_thread(
                    table.add_row,
                    side,
                    p_sym,
                    fmt(ev["baseValue"], precision=2),
                    fmt(ev["tokenValue"], precision=2),
                    fmt(ev["fee"], precision=2),
                    trader,
                    str(ev["block"]),
                )

            self._update_chart()
        except Exception as e:
            self._log(f"Error loading trades: {e}")

    def _update_chart(self) -> None:
        player = self.token_data.player_by_symbol.get(self.selected_token)
        if not player:
            return

        token_addr = player["address"].lower()
        prices = []
        for ev in self.trade_history:
            if ev["token"].lower() == token_addr and ev["tokenValue"] > 0:
                price = float(Decimal(ev["baseValue"]) / Decimal(ev["tokenValue"]))
                prices.append(price)

        chart = self.query_one("#price-chart", PriceChart)
        self.call_from_thread(chart.set_data, prices, f"{player['name']} ({player['symbol']})")

    # --- Actions ---

    def action_refresh(self) -> None:
        self._do_refresh()

    @work(thread=True)
    def _do_refresh(self) -> None:
        self._log("Refreshing...")
        self._refresh_data()
        self._log("✓ Refreshed")

    def action_focus_buy(self) -> None:
        self.query_one("#trade-token", Input).focus()

    def action_focus_sell(self) -> None:
        self.query_one("#trade-token", Input).focus()

    def action_select_tab_portfolio(self) -> None:
        self.query_one(TabbedContent).active = "tab-portfolio"

    def action_select_tab_trades(self) -> None:
        self.query_one(TabbedContent).active = "tab-trades"

    # --- Trading ---

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-buy":
            self._execute_trade("buy")
        elif event.button.id == "btn-sell":
            self._execute_trade("sell")
        elif event.button.id == "btn-max":
            self._set_max_amount()

    @work(thread=True)
    def _set_max_amount(self) -> None:
        """Set amount to max balance of the relevant token."""
        if not self.w3 or not self.account:
            return
        token_input = self.query_one("#trade-token", Input).value.strip().upper()
        player = self.token_data.player_by_symbol.get(token_input)
        if not player:
            self._log(f"Unknown token: {token_input}")
            return

        country = self.token_data.get_country_for_player(token_input)
        if not country:
            self._log(f"No country mapping for {token_input}")
            return

        try:
            # For buy: max country token balance (floor to integer)
            bal = chain.get_balance(self.w3, country["address"], self.account.address)
            # Floor to whole number to avoid reverts
            whole = (bal // WEI) * WEI
            if whole > 0:
                amount_str = str(whole // WEI)
                inp = self.query_one("#trade-amount", Input)
                self.call_from_thread(setattr, inp, "value", amount_str)
                self._log(f"MAX: {amount_str} {country['symbol']}")
            else:
                self._log(f"No {country['symbol']} balance")
        except Exception as e:
            self._log(f"Error: {e}")

    @work(thread=True)
    def _execute_trade(self, side: str) -> None:
        if not self.w3 or not self.account:
            self._log("✗ Not connected or no private key")
            return

        token_input = self.query_one("#trade-token", Input).value.strip().upper()
        amount_input = self.query_one("#trade-amount", Input).value.strip()
        slippage_input = self.query_one("#trade-slippage", Input).value.strip()

        if not token_input or not amount_input:
            self._log("✗ Fill in token and amount")
            return

        player = self.token_data.player_by_symbol.get(token_input)
        if not player:
            self._log(f"✗ Unknown token: {token_input}")
            return

        country = self.token_data.get_country_for_player(token_input)
        if not country:
            self._log(f"✗ No country mapping for {token_input}")
            return

        try:
            slippage = float(slippage_input) / 100
        except ValueError:
            slippage = 0.05

        try:
            amount_wei = int(float(amount_input) * WEI)
        except ValueError:
            self._log("✗ Invalid amount")
            return

        try:
            if side == "buy":
                spend_token = country["address"]
                self._log(f"→ BUY {token_input} for {amount_input} {country['symbol']}")

                self._log("  Checking allowance...")
                chain.ensure_allowance(self.w3, self.account, spend_token, config.ROUTER, amount_wei)

                # min_out: rough estimate with slippage
                min_out = int(amount_wei * (1 - slippage) * 0.5)
                self._log(f"  Sending tx... (slippage: {slippage*100:.0f}%)")

                tx_hash, receipt = chain.execute_buy(
                    self.w3, self.account, player["address"], amount_wei, min_out
                )
            else:
                self._log(f"→ SELL {amount_input} {token_input}")

                self._log("  Checking allowance...")
                chain.ensure_allowance(self.w3, self.account, player["address"], config.ROUTER, amount_wei)

                min_out = int(amount_wei * (1 - slippage) * 0.1)
                self._log(f"  Sending tx... (slippage: {slippage*100:.0f}%)")

                tx_hash, receipt = chain.execute_sell(
                    self.w3, self.account, player["address"], amount_wei, min_out
                )

            if receipt.status == 1:
                self._log(f"  ✓ SUCCESS | tx: {tx_hash.hex()[:20]}...")
            else:
                self._log(f"  ✗ REVERTED | tx: {tx_hash.hex()[:20]}...")

            _time.sleep(2)
            self._refresh_data()

        except Exception as e:
            err = str(e)
            if len(err) > 120:
                err = err[:120] + "..."
            self._log(f"  ✗ Error: {err}")

    # --- Row selection ---
    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table = event.data_table
        row_data = table.get_row(event.row_key)
        if not row_data:
            return

        symbol = None
        if table.id == "trades-table" and len(row_data) >= 2:
            symbol = str(row_data[1]).strip()
        elif table.id == "markets-table" and len(row_data) >= 2:
            symbol = str(row_data[1]).strip()
            # Also fill trade token input
            try:
                inp = self.query_one("#trade-token", Input)
                inp.value = symbol
            except Exception:
                pass

        if symbol and symbol in self.token_data.player_by_symbol:
            self.selected_token = symbol
            self._update_chart_sync()

    def _update_chart_sync(self) -> None:
        player = self.token_data.player_by_symbol.get(self.selected_token)
        if not player:
            return
        token_addr = player["address"].lower()
        prices = []
        for ev in self.trade_history:
            if ev["token"].lower() == token_addr and ev["tokenValue"] > 0:
                price = float(Decimal(ev["baseValue"]) / Decimal(ev["tokenValue"]))
                prices.append(price)
        chart = self.query_one("#price-chart", PriceChart)
        chart.set_data(prices, f"{player['name']} ({player['symbol']})")


if __name__ == "__main__":
    PitchTerminal().run()
