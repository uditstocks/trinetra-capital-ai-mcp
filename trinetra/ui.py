"""Financial-grade terminal UI (rich-powered, plain-text fallback).

Every user-facing element of the CLI goes through this module. Money data is
rendered ONLY from structured tool payloads — never from LLM prose — so what
the user sees is exactly what the broker returned. If `rich` is not installed
everything degrades to the plain markdown renderers in `trinetra.render`.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from typing import Any

from trinetra import render
from trinetra.config import AuthMethod, settings

# Windows consoles/pipes often default to cp1252, which cannot encode ₹/emoji
# and crashes the renderer mid-session. Force UTF-8 (never fatal on failure).
if sys.platform == "win32":  # pragma: no cover - exercised only on Windows
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

try:
    from rich import box
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.markup import escape as _esc
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    _console: Any = Console(highlight=False)
except ImportError:  # pragma: no cover - rich is in requirements
    _console = None

    def _esc(s: str) -> str:  # type: ignore[misc]
        return s


def has_rich() -> bool:
    return _console is not None


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
def info(msg: str) -> None:
    if _console:
        _console.print(f"[dim]{_esc(msg)}[/dim]")
    else:
        print(msg)


def success(msg: str) -> None:
    if _console:
        _console.print(f"[bold green]✓[/bold green] {_esc(msg)}")
    else:
        print(f"✓ {msg}")


def warn(msg: str) -> None:
    if _console:
        _console.print(f"[bold yellow]⚠[/bold yellow] {_esc(msg)}")
    else:
        print(f"⚠ {msg}")


def error(msg: str) -> None:
    if _console:
        _console.print(f"[bold red]✗[/bold red] {_esc(msg)}")
    else:
        print(f"✗ {msg}")


def timing(seconds: float, note: str = "") -> None:
    suffix = f" • {note}" if note else ""
    if _console:
        _console.print(f"[dim]{seconds:.1f}s{suffix}[/dim]")
    else:
        print(f"({seconds:.1f}s{suffix})")


def markdown(text: str) -> None:
    """Render agent prose/markdown (tables included) nicely."""
    if _console:
        _console.print(Markdown(text))
    else:
        print("\n" + text.strip() + "\n")


def ask(prompt: str = "🔱 ❯ ") -> str:
    """Styled input prompt. Raises EOFError/KeyboardInterrupt like input()."""
    if _console:
        return _console.input(f"[bold cyan]{prompt}[/bold cyan]")
    return input(prompt)


@contextmanager
def status(message: str):
    """Spinner while a slow step runs (no-op fallback)."""
    if _console:
        with _console.status(f"[bold cyan]{message}[/bold cyan]", spinner="dots"):
            yield
    else:
        print(f"… {message}")
        yield


# --------------------------------------------------------------------------- #
# banner / session chrome
# --------------------------------------------------------------------------- #
def banner() -> None:
    mode_live = settings.is_live
    if not _console:
        line = "=" * 64
        print(line)
        print("  🔱  TRINETRA CAPITAL AI  —  Multi-Agent Trading (Groww)")
        print(line)
        print(f"  MODE: {'LIVE — real money!' if mode_live else 'PAPER — simulated'}")
        print(f"  Safety cap: ₹{settings.max_order_value:,.0f} per order"
              f"  |  Default product: {settings.default_product}")
        print(line)
        return

    mode = Text("LIVE — orders hit your REAL Groww account", style="bold red") \
        if mode_live else Text("PAPER — simulated, no real money", style="bold green")
    if settings.groww_configured:
        method = "TOTP" if settings.auth_method is AuthMethod.TOTP else "API key + secret"
        groww = Text(f"connected ({method}) — market data & portfolio are live", style="green")
    else:
        groww = Text("not connected — yfinance data + paper portfolio "
                     "(run `python connect_groww.py`)", style="yellow")

    body = Table.grid(padding=(0, 1))
    body.add_column(justify="right", style="bold dim")
    body.add_column()
    body.add_row("Mode", mode)
    body.add_row("Groww", groww)
    body.add_row("Safety cap", f"₹{settings.max_order_value:,.0f} per order")
    body.add_row("Product", f"{settings.default_product} (default)  •  "
                            f"exchange {settings.default_exchange}")
    _console.print(Panel(
        body,
        title="[bold]🔱  TRINETRA CAPITAL AI[/bold]",
        subtitle="[dim]multi-agent trading desk[/dim]",
        border_style="red" if mode_live else "cyan",
        box=box.DOUBLE,
    ))


# --------------------------------------------------------------------------- #
# money renderers (deterministic — payloads in, pixels out)
# --------------------------------------------------------------------------- #
def _pnl_text(val: Any, fmt: str) -> "Text":
    if val is None:
        return Text("—", style="dim")
    v = float(val)
    style = "green" if v >= 0 else "red"
    sign = "+" if v >= 0 else "−"
    return Text(f"{sign}{fmt.format(abs(v))}", style=style)


def portfolio_view(payload: dict[str, Any]) -> None:
    """Render a view_portfolio payload: holdings table + summary + cash."""
    if payload.get("error"):
        error(f"Portfolio unavailable: {payload['error']}")
        return
    if not _console:
        print("\n" + (payload.get("display") or render.render_portfolio(payload)) + "\n")
        return

    mode = payload.get("mode", "paper")
    holdings = payload.get("holdings", [])
    summary = payload.get("summary", {}) or {}
    funds = payload.get("funds", {}) or {}
    title = "Portfolio — " + ("[red]LIVE[/red]" if mode == "live" else "[green]PAPER[/green]")

    if not holdings:
        cash = funds.get("available_cash")
        msg = "No holdings yet."
        if cash is not None:
            msg += f"\nAvailable cash: ₹{float(cash):,.2f}"
        _console.print(Panel(msg, title=title, border_style="cyan"))
        return

    # Compact layout so money values never truncate, even at 80 cols (per-row
    # invested is qty × avg; the totals are in the summary line below).
    table = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold",
                  title_justify="left", pad_edge=False)
    table.add_column("Symbol", style="bold", no_wrap=True)
    table.add_column("Qty", justify="right", no_wrap=True)
    table.add_column("Avg", justify="right", no_wrap=True)
    table.add_column("LTP", justify="right", no_wrap=True)
    table.add_column("Value", justify="right", no_wrap=True)
    table.add_column("P&L (%)", justify="right", no_wrap=True)   # amount + % merged
    table.add_column("Alloc", justify="right", no_wrap=True)
    table.add_column("Days", justify="right", no_wrap=True)

    def n(x: Any) -> str:
        return f"{float(x):,.2f}" if x is not None else "—"

    unpriced: list[str] = []
    for h in holdings:
        if h.get("last_price") is None:
            unpriced.append(h.get("trading_symbol", "?"))
        alloc = h.get("allocation_pct")
        pnl_cell = _pnl_text(h.get("pnl"), "₹{:,.0f}")
        if h.get("pnl_pct") is not None:
            pnl_cell.append_text(Text(f" ({_fmt_pct(h.get('pnl_pct'))})",
                                      style=pnl_cell.style or ""))
        table.add_row(
            str(h.get("trading_symbol", "?")),
            str(h.get("quantity", "—")),
            n(h.get("average_price")),
            n(h.get("last_price")),
            f"₹{float(h.get('current_value')):,.0f}" if h.get("current_value") is not None else "—",
            pnl_cell,
            f"{float(alloc):.0f}%" if alloc is not None else "—",
            str(h.get("holding_days")) if h.get("holding_days") is not None else "—",
        )
    _console.print(table)

    # Summary line: unrealized always; booked + overall when there's realized P&L.
    line = Text.assemble(
        ("Invested ", "bold dim"), (f"₹{float(summary.get('total_invested', 0)):,.2f}", ""),
        ("   Value ", "bold dim"), (f"₹{float(summary.get('current_value', 0)):,.2f}", ""),
        ("   Unrealized ", "bold dim"),
    )
    line.append_text(_pnl_text(summary.get("total_pnl"), "₹{:,.2f}"))
    if summary.get("realized_pnl"):
        line.append_text(Text("   Booked ", style="bold dim"))
        line.append_text(_pnl_text(summary.get("realized_pnl"), "₹{:,.2f}"))
        line.append_text(Text("   Overall ", style="bold dim"))
        line.append_text(_pnl_text(summary.get("overall_pnl"), "₹{:,.2f}"))
    if funds.get("available_cash") is not None:
        line.append_text(Text.assemble(
            ("   Cash ", "bold dim"), (f"₹{float(funds['available_cash']):,.2f}", "")))
    _console.print(line)

    alerts = summary.get("concentration_alert")
    if alerts:
        warn(f"Concentration: {', '.join(alerts)} exceed 25% of the portfolio — "
             "consider diversifying.")
    if unpriced:
        info(f"Live price unavailable for {', '.join(unpriced)}; its P&L is excluded.")


def orders_view(payload: dict[str, Any]) -> None:
    """Render a get_order_history payload."""
    if payload.get("error"):
        error(f"Order history unavailable: {payload['error']}")
        return
    orders = payload.get("orders", [])
    mode = payload.get("mode", "paper")
    if not _console:
        print("\n" + (payload.get("display") or render.render_orders(orders, mode)) + "\n")
        return
    if not orders:
        _console.print(Panel("No orders found.", title="Orders", border_style="cyan"))
        return

    title = "Orders — " + ("[red]LIVE[/red]" if mode == "live" else "[green]PAPER[/green]")
    table = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold",
                  title_justify="left", pad_edge=False)
    for col, justify in (("When", "left"), ("Symbol", "left"), ("Side", "center"),
                         ("Qty", "right"), ("Price", "right"), ("Type", "left"),
                         ("Status", "left")):
        table.add_column(col, justify=justify)
    for o in orders:
        r = render.order_row(o)
        side_style = "green" if r["side"] == "BUY" else "red" if r["side"] == "SELL" else ""
        status_l = r["status"].lower()
        status_style = ("green" if status_l in ("filled", "executed", "complete", "completed")
                        else "red" if status_l in ("rejected", "failed", "cancelled")
                        else "yellow")
        table.add_row(r["when"], Text(r["symbol"], style="bold"),
                      Text(r["side"], style=side_style), r["qty"], r["price"], r["type"],
                      Text(r["status"], style=status_style))
    _console.print(table)


def funds_view(payload: dict[str, Any]) -> None:
    if payload.get("error"):
        error(f"Funds unavailable: {payload['error']}")
        return
    mode = payload.get("mode", "paper")
    if not _console:
        print(f"\nAvailable cash: ₹{payload.get('available_cash', 0):,.2f}  "
              f"(margin used: ₹{payload.get('margin_used', 0):,.2f}, mode: {mode})\n")
        return
    body = Table.grid(padding=(0, 1))
    body.add_column(justify="right", style="bold dim")
    body.add_column(justify="left")
    body.add_row("Available cash", f"₹{float(payload.get('available_cash', 0)):,.2f}")
    body.add_row("Margin used", f"₹{float(payload.get('margin_used', 0)):,.2f}")
    if payload.get("net") is not None:
        body.add_row("Net worth" if mode == "paper" else "Net",
                     f"₹{float(payload['net']):,.2f}")
    title = "Funds — " + ("[red]LIVE[/red]" if mode == "live" else "[green]PAPER[/green]")
    _console.print(Panel(body, title=title, border_style="cyan", title_align="left"))


def _signed(val: Any, fmt: str = "₹{:,.2f}") -> "Text":
    return _pnl_text(val, fmt)


def performance_view(payload: dict[str, Any]) -> None:
    """Render the get_performance payload: booked P&L, win/loss stats, best/worst,
    closed positions, today's P&L and sector allocation."""
    if payload.get("error") or not payload.get("available"):
        if not _console:
            print("\n" + (payload.get("display") or
                          f"Performance unavailable: {payload.get('error','n/a')}") + "\n")
        else:
            warn(payload.get("error", "Performance analytics are unavailable."))
        return
    if not _console:
        print("\n" + (payload.get("display") or render.render_performance(payload)) + "\n")
        return

    stats = payload.get("stats", {}) or {}
    today = payload.get("today", {}) or {}

    # Headline: the three P&L numbers the user cares about.
    head = Table.grid(padding=(0, 2))
    head.add_column(justify="right", style="bold dim")
    head.add_column(justify="left")
    head.add_row("Booked P&L (net)", _signed(payload.get("total_realized")))
    head.add_row("  ↳ profit booked", _signed(stats.get("gross_profit")))
    head.add_row("  ↳ loss booked", _signed(stats.get("gross_loss")))
    head.add_row("Unrealized (open)", _signed(payload.get("unrealized")))
    overall = Text.assemble(_signed(payload.get("overall_pnl")))
    head.add_row("Overall P&L", overall)
    if today:
        head.add_row("Today", _signed(today.get("total")))
    _console.print(Panel(head, title="[bold]Trading Performance — [green]PAPER[/green][/bold]",
                         border_style="cyan", title_align="left"))

    # Win/loss stats.
    if stats.get("closed_trades"):
        s = Text.assemble(
            ("Trades ", "bold dim"), (f"{stats.get('closed_trades')}", ""),
            ("   Win rate ", "bold dim"),
            (f"{stats.get('win_rate')}%", "green" if (stats.get("win_rate") or 0) >= 50 else "red"),
            ("   ", ""), (f"{stats.get('winners')}W", "green"), (" / ", "dim"),
            (f"{stats.get('losers')}L", "red"),
            ("   Avg win ", "bold dim"),
        )
        s.append_text(_signed(stats.get("avg_win")))
        s.append_text(Text("   Avg loss ", style="bold dim"))
        s.append_text(_signed(stats.get("avg_loss")))
        _console.print(s)

        best, worst = payload.get("best_trade"), payload.get("worst_trade")
        if best:
            b = Text.assemble(("Best  ", "bold dim"), (best["symbol"] + "  ", "bold"))
            b.append_text(_signed(best["realized"]))
            b.append_text(Text(f"  ({_fmt_pct(best.get('realized_pct'))})", style="dim"))
            _console.print(b)
        if worst and worst is not best:
            w = Text.assemble(("Worst ", "bold dim"), (worst["symbol"] + "  ", "bold"))
            w.append_text(_signed(worst["realized"]))
            w.append_text(Text(f"  ({_fmt_pct(worst.get('realized_pct'))})", style="dim"))
            _console.print(w)
    else:
        info("No closed trades yet — booked P&L appears once you sell.")

    # Realized by symbol.
    by_sym = payload.get("realized_by_symbol") or {}
    if by_sym:
        t = Table(title="Booked P&L by stock", box=box.SIMPLE, header_style="bold",
                  title_justify="left", pad_edge=False)
        t.add_column("Symbol", style="bold")
        t.add_column("Booked P&L", justify="right")
        for sym, val in sorted(by_sym.items(), key=lambda kv: kv[1], reverse=True):
            t.add_row(sym, _signed(val))
        _console.print(t)

    # Closed positions.
    closed = payload.get("closed_positions") or []
    if closed:
        cp = Text("Closed positions: ", style="bold dim")
        for i, c in enumerate(closed):
            if i:
                cp.append_text(Text("  ", style="dim"))
            cp.append_text(Text(c["symbol"] + " ", style="bold"))
            cp.append_text(_signed(c["realized"]))
        _console.print(cp)

    # Sector allocation.
    sectors = payload.get("sector_breakdown") or []
    if sectors:
        st = Table(title="Sector allocation", box=box.SIMPLE, header_style="bold",
                   title_justify="left", pad_edge=False)
        st.add_column("Sector", style="bold")
        st.add_column("Value", justify="right")
        st.add_column("Weight", justify="right")
        for s in sectors:
            st.add_row(str(s["sector"]),
                       f"₹{float(s['value']):,.2f}",
                       f"{float(s['pct']):.0f}%" if s.get("pct") is not None else "—")
        _console.print(st)


def _fmt_pct(x: Any) -> str:
    if x is None:
        return "—"
    v = float(x)
    return f"{'+' if v >= 0 else '−'}{abs(v):,.2f}%"


# --------------------------------------------------------------------------- #
# self-introduction / help
# --------------------------------------------------------------------------- #
SERVICES = [
    ("📊  Research & quotes",
     "Live prices, company fundamentals, market cap, 52-week range, symbol lookup.",
     ['"price of Reliance"', '"tell me about INFY"', '"look up ICICI"']),
    ("🧠  Analysis & advice",
     "Technical + news-sentiment read (RSI, MACD, Bollinger, ATR) with a BUY/SELL/HOLD "
     "signal, stop-loss and targets.",
     ['"should I buy TCS?"', '"what\'s the outlook for HDFC Bank?"']),
    ("💹  Trading desk",
     "Place, modify or cancel buy/sell orders (market, limit, stop-loss). Every order "
     "pauses for your explicit approval, under a hard per-order safety cap.",
     ['"buy 10 Infosys"', '"invest ₹10,000 in ITC"', '"sell 5 Reliance at 1400 limit"']),
    ("📁  Portfolio & funds",
     "Holdings with live P&L, allocation %, holding period and concentration alerts; "
     "available cash / buying power.",
     ['"portfolio"', '"funds"', '"orders"']),
    ("🏆  Performance & booked P&L",
     "Realized (booked) profit & loss to date, win rate, best/worst trades, closed "
     "positions, today's P&L and sector mix.",
     ['"performance"', '"how much profit have I booked?"', '"am I net positive?"']),
]


def introduce() -> None:
    """A professional self-introduction: who the agent is, what it does, and the
    exact prompts to access each service. Rendered deterministically (no LLM)."""
    from trinetra.config import settings

    mode = "LIVE (real Groww account)" if settings.is_live else "PAPER (simulated, no real money)"
    if not _console:
        print("\n🔱 TRINETRA CAPITAL AI — your multi-agent trading desk assistant.")
        print(f"Mode: {mode}\n\nServices:")
        for name, desc, examples in SERVICES:
            print(f"\n{name}\n  {desc}\n  Try: {', '.join(examples)}")
        print("\nType a request in plain English. 'exit' to quit.\n")
        return

    intro = Text.assemble(
        ("I am Trinetra Capital AI", "bold"),
        (" — a multi-agent trading-desk assistant for Indian markets (NSE/BSE) "
         "via Groww. I coordinate three specialists — research, analysis and a "
         "trading desk — to answer your questions and execute trades on your "
         "instruction, safely and with your approval.\n\n", ""),
        ("Current mode: ", "bold dim"),
        (mode, "red" if settings.is_live else "green"),
    )
    _console.print(Panel(intro, title="[bold]🔱  Trinetra Capital AI[/bold]",
                         subtitle="[dim]at your service[/dim]",
                         border_style="cyan", box=box.DOUBLE))

    table = Table(box=box.SIMPLE_HEAVY, header_style="bold", pad_edge=False, show_lines=True)
    table.add_column("Service", style="bold", no_wrap=True)
    table.add_column("What it does")
    table.add_column("How to ask", style="cyan")
    for name, desc, examples in SERVICES:
        table.add_row(name, desc, "\n".join(examples))
    _console.print(table)
    _console.print(Text.assemble(
        ("Just type what you want in plain English", "bold"),
        (" — I route it to the right specialist. Shortcuts: ", "dim"),
        ("portfolio, performance, funds, orders, help", "cyan"),
        ("  •  'exit' to quit.", "dim"),
    ))


_OK_STATUSES = ("filled", "placed", "open", "accepted", "approved", "modified",
                "cancelled", "cancel_requested", "new")


def order_result_view(name: str, payload: dict[str, Any]) -> None:
    """Render the result of place/modify/cancel_order deterministically."""
    status = str(payload.get("status", "unknown")).lower()
    failed = bool(payload.get("error")) or status in ("rejected", "failed", "error")

    if not _console:
        if failed:
            print(f"\n✗ {name} {status.upper()}: {payload.get('error', '')}\n")
        else:
            print(f"\n✓ {name} {status.upper()}: {json.dumps(payload, default=str)}\n")
        return

    if failed:
        detail = str(payload.get("error") or payload.get("message") or "no detail")
        _console.print(Panel(
            f"[bold red]{_esc(status.upper())}[/bold red] — {_esc(detail)}",
            title=f"Order {'not placed' if name == 'place_order' else 'action failed'}",
            border_style="red", title_align="left"))
        return

    body = Table.grid(padding=(0, 1))
    body.add_column(justify="right", style="bold dim")
    body.add_column(justify="left")
    side = str(payload.get("transaction_type", "")).upper()
    if payload.get("trading_symbol"):
        side_style = "green" if side == "BUY" else "red"
        body.add_row("Order", Text.assemble(
            (side or "?", side_style), " ",
            (str(payload.get("quantity", "?")), "bold"), " × ",
            (str(payload.get("trading_symbol")), "bold"),
            f"  ({payload.get('order_type', '?')}, {payload.get('product', '?')})"))
    px = payload.get("average_price") or payload.get("price")
    if px:
        body.add_row("Price", f"₹{float(px):,.2f}")
    if payload.get("trigger_price"):
        body.add_row("Trigger", f"₹{float(payload['trigger_price']):,.2f}")
    if payload.get("estimated_value"):
        body.add_row("Value", f"₹{float(payload['estimated_value']):,.2f}")
    if payload.get("order_id"):
        body.add_row("Order ID", Text(str(payload["order_id"])))
    if payload.get("message"):
        body.add_row("Note", Text(str(payload["message"])))
    mode = payload.get("trading_mode") or payload.get("mode", "")
    title = (f"[bold green]✓ {status.upper()}[/bold green]"
             + (f" — [red]LIVE[/red]" if mode == "live" else f" — [green]{mode.upper()}[/green]" if mode else ""))
    _console.print(Panel(body, title=title, border_style="green", title_align="left"))


def approval_panel(lines: list[str], live: bool) -> None:
    """The HITL approval prompt: exactly what is about to be executed. Content is
    markup-escaped — nothing the LLM put in the order args can be dropped/styled."""
    text = "\n".join(lines)
    if not _console:
        print("\n--- ⚠️ APPROVAL NEEDED ---")
        print(text)
        if live:
            print(">>> THIS IS A REAL ORDER ON YOUR LIVE GROWW ACCOUNT <<<")
        return
    text = _esc(text)
    if live:
        text += "\n\n[bold red]THIS IS A REAL ORDER ON YOUR LIVE GROWW ACCOUNT[/bold red]"
    _console.print(Panel(
        text,
        title="[bold yellow]⚠ Approval needed[/bold yellow]",
        border_style="red" if live else "yellow",
        title_align="left",
    ))
