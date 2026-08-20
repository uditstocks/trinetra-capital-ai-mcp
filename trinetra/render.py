"""Deterministic, human-readable renderers.

Formatting the portfolio in Python (not in the LLM) guarantees a clean, stable
table every time and removes a slow, unreliable formatting round-trip. The
agent is instructed to relay the `display` string verbatim.
"""

from __future__ import annotations

from typing import Any


def _grouped(value: float, decimals: int = 2) -> str:
    """Indian digit grouping: 1,00,000 — last three, then pairs.

    Western grouping reads wrong to anyone thinking in lakhs and crores, which is
    everyone this product is for. Shared shape with charts._rupees so a number
    looks identical in a table and on a chart.
    """
    text = f"{abs(value):.{decimals}f}"
    whole, _, fraction = text.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        pairs = []
        while len(head) > 2:
            pairs.insert(0, head[-2:])
            head = head[:-2]
        if head:
            pairs.insert(0, head)
        whole = ",".join([*pairs, tail])
    return whole + (f".{fraction}" if fraction else "")


def _money(x: Any) -> str:
    if x is None:
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return f"{'−' if v < 0 else ''}₹{_grouped(v)}"


def _num(x: Any) -> str:
    if x is None:
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return f"{'−' if v < 0 else ''}{_grouped(v)}"


def _signed_money(x: Any) -> str:
    if x is None:
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return f"{'+' if v >= 0 else '−'}₹{_grouped(v)}"


def _signed_pct(x: Any) -> str:
    if x is None:
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return f"{'+' if v >= 0 else '−'}{abs(v):,.2f}%"


def render_portfolio(data: dict[str, Any]) -> str:
    """Render the structured view_portfolio payload into a markdown report."""
    mode = data.get("mode", "paper")
    holdings = data.get("holdings", [])
    summary = data.get("summary", {})

    mode_label = "PAPER (simulated)" if mode == "paper" else "LIVE (real Groww account)"
    lines = [f"**Portfolio — {mode_label}**", ""]

    if not holdings:
        lines.append("_No holdings yet._")
        funds = data.get("funds")
        if funds:
            lines.append(f"\nAvailable cash: {_money(funds.get('available_cash'))}")
        return "\n".join(lines)

    lines.append("| # | Symbol | Qty | Avg | LTP | Value | P&L | P&L % | Alloc | Days |")
    lines.append("|--:|:-------|----:|----:|----:|------:|----:|------:|-----:|-----:|")
    for i, h in enumerate(holdings, 1):
        lines.append(
            f"| {i} | **{h.get('trading_symbol','?')}** "
            f"| {h.get('quantity','—')} "
            f"| {_num(h.get('average_price'))} "
            f"| {_num(h.get('last_price'))} "
            f"| {_money(h.get('current_value'))} "
            f"| {_signed_money(h.get('pnl'))} "
            f"| {_signed_pct(h.get('pnl_pct'))} "
            f"| {_num(h.get('allocation_pct'))}% "
            f"| {h.get('holding_days','—')} |"
        )

    total_pnl = summary.get("total_pnl")
    arrow = "▲" if (total_pnl or 0) >= 0 else "▼"
    parts = [
        f"**Invested:** {_money(summary.get('total_invested'))}",
        f"**Value:** {_money(summary.get('current_value'))}",
        f"**Unrealized:** {arrow} {_signed_money(total_pnl)}",
    ]
    realized = summary.get("realized_pnl")
    if realized:
        parts.append(f"**Booked:** {_signed_money(realized)}")
        parts.append(f"**Overall:** {_signed_money(summary.get('overall_pnl'))}")
    parts.append(f"**Holdings:** {summary.get('holdings_count', len(holdings))}")
    lines += ["", "  •  ".join(parts)]

    alerts = summary.get("concentration_alert")
    if alerts:
        lines.append(f"\n_⚠ Concentration: {', '.join(alerts)} exceed 25% of the "
                     f"portfolio — consider diversifying._")

    unpriced = [h.get("trading_symbol") for h in holdings if h.get("last_price") is None]
    if unpriced:
        lines.append(
            f"\n_Note: live price unavailable for {', '.join(unpriced)} "
            f"(symbol may be delisted/non-NSE); its P&L is excluded._"
        )
    return "\n".join(lines)


def render_performance(data: dict[str, Any]) -> str:
    """Render the get_performance payload (booked P&L + trade stats) as markdown."""
    if not data.get("available"):
        return f"_Performance analytics unavailable: {data.get('error', 'n/a')}_"

    stats = data.get("stats", {}) or {}
    lines = ["**Trading Performance — PAPER (simulated)**", ""]

    # Headline booked P&L.
    lines += [
        f"**Booked P&L (net):** {_signed_money(data.get('total_realized'))}  •  "
        f"**Unrealized:** {_signed_money(data.get('unrealized'))}  •  "
        f"**Overall:** {_signed_money(data.get('overall_pnl'))}",
        "",
        f"Profit booked: {_signed_money(stats.get('gross_profit'))}  •  "
        f"Loss booked: {_signed_money(stats.get('gross_loss'))}",
    ]

    # Win/loss stats.
    if stats.get("closed_trades"):
        wr = stats.get("win_rate")
        lines += [
            "",
            f"**Trades:** {stats.get('closed_trades')} closed  •  "
            f"**Win rate:** {_num(wr)}%  ({stats.get('winners')}W / {stats.get('losers')}L)  •  "
            f"**Avg win:** {_signed_money(stats.get('avg_win'))}  •  "
            f"**Avg loss:** {_signed_money(stats.get('avg_loss'))}",
        ]
        best, worst = data.get("best_trade"), data.get("worst_trade")
        if best:
            lines.append(f"**Best:** {best['symbol']} {_signed_money(best['realized'])} "
                         f"({_signed_pct(best.get('realized_pct'))})")
        if worst and worst is not best:
            lines.append(f"**Worst:** {worst['symbol']} {_signed_money(worst['realized'])} "
                         f"({_signed_pct(worst.get('realized_pct'))})")
    else:
        lines += ["", "_No closed trades yet — booked P&L appears once you sell._"]

    # Today.
    today = data.get("today") or {}
    if today:
        lines.append("")
        lines.append(f"**Today's P&L:** {_signed_money(today.get('total'))}  "
                     f"(booked {_signed_money(today.get('realized'))}, "
                     f"unrealized {_signed_money(today.get('unrealized'))})")

    # Realized by symbol.
    by_sym = data.get("realized_by_symbol") or {}
    if by_sym:
        lines += ["", "| Symbol | Booked P&L |", "|:-------|-----------:|"]
        for sym, val in sorted(by_sym.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"| **{sym}** | {_signed_money(val)} |")

    # Closed positions.
    closed = data.get("closed_positions") or []
    if closed:
        lines += ["", "**Closed positions (fully exited):**"]
        for c in closed:
            lines.append(f"- {c['symbol']}: {_signed_money(c['realized'])}")

    # Sector allocation.
    sectors = data.get("sector_breakdown") or []
    if sectors:
        lines += ["", "**Sector allocation:**"]
        for s in sectors:
            lines.append(f"- {s['sector']}: {_money(s['value'])} ({_num(s.get('pct'))}%)")

    return "\n".join(lines)


def order_row(o: dict[str, Any]) -> dict[str, str]:
    """Normalise one order dict (paper trade-log shape or Groww API shape) into
    ready-to-display string fields. Single source of truth for the markdown and
    rich renderers."""
    when = o.get("timestamp") or o.get("created_at") or o.get("order_time") or "—"
    if isinstance(when, str) and "T" in when:
        when = when.split(".")[0].replace("T", " ")
    return {
        "when": str(when),
        "symbol": str(o.get("symbol") or o.get("trading_symbol") or "—"),
        "side": str(o.get("action") or o.get("transaction_type") or "—").upper(),
        "qty": str(o.get("shares") or o.get("quantity") or "—"),
        "price": _num(o.get("price") or o.get("average_price")),
        "type": str(o.get("order_type") or o.get("type") or "—"),
        "status": str(o.get("status") or o.get("order_status") or "filled"),
    }


def render_orders(orders: list[dict[str, Any]], mode: str = "paper") -> str:
    """Render an order history / book list into a markdown table."""
    if not orders:
        return "_No orders found._"

    lines = ["| When | Symbol | Side | Qty | Price | Type | Status |",
             "|:-----|:-------|:----:|----:|------:|:-----|:-------|"]
    for o in orders:
        r = order_row(o)
        lines.append(
            f"| {r['when']} | **{r['symbol']}** | {r['side']} | {r['qty']} "
            f"| {r['price']} | {r['type']} | {r['status']} |"
        )
    return "\n".join(lines)
