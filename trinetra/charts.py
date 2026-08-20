"""Chart rendering — PNGs returned alongside the numbers.

A table tells you what you hold; a chart tells you the shape of it at a glance.
These render server-side so the picture is always the same picture, computed from
the same figures the tables show.

Design rules followed here, and why:
- **Allocation is one series, so it is one colour.** Shading bars by size would
  double-encode length as hue and burn the only free channel.
- **P&L uses the reserved status pair** (green gain / red loss) because the colour
  genuinely means good-or-bad — and every bar carries its signed value as a label,
  so the meaning never rests on colour alone (which matters for the ~8% of men
  with red-green colour blindness).
- **Never two y-scales on one plot.** Price and RSI are separate stacked panels
  sharing an x-axis; overlaying them would invent a correlation.
- Recessive grid, no top/right spines, thin marks, selective direct labels.
"""

from __future__ import annotations

import io
from typing import Any

import matplotlib

matplotlib.use("Agg")  # no display on a server
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

# --- palette -------------------------------------------------------------- #
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_MUTED = "#52514e"
GRID = "#e6e5e1"
SERIES_1 = "#2a78d6"   # categorical slot 1 — identity, not magnitude
GAIN = "#0ca30c"       # status: good
LOSS = "#d03b3b"       # status: critical
NEUTRAL = "#c9c8c3"
BAND = "#dce9f9"       # blue at low opacity, for the Bollinger envelope

DPI = 110
BAR_HEIGHT = 0.62


def _group_indian(digits: str) -> str:
    """Group digits the Indian way: 1,00,000 — last three, then pairs.

    Western grouping (100,000) is read wrong at a glance by anyone who thinks in
    lakhs and crores, which is everyone this product is for.
    """
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    pairs = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    if head:
        pairs.insert(0, head)
    return ",".join([*pairs, tail])


def _rupees(value: float, decimals: int = 0) -> str:
    """₹1,00,000 / −₹1,200 — sign leads, so it is never read as part of the amount."""
    sign = "−" if value < 0 else ""
    text = f"{abs(value):.{decimals}f}"
    whole, _, fraction = text.partition(".")
    return f"{sign}₹{_group_indian(whole)}" + (f".{fraction}" if fraction else "")


def _signed(value: float) -> str:
    return f"{'+' if value >= 0 else '−'}₹{abs(value):,.0f}"


def _tone(value: float) -> str:
    """Gain green, loss red, and neutral for exactly zero — an unchanged
    position has not made money, so it should not be coloured as if it had."""
    if value > 0:
        return GAIN
    return LOSS if value < 0 else INK_MUTED


_MONEY_AXIS = FuncFormatter(lambda v, _: _rupees(v))


def _style_axes(ax, *, xgrid: bool = True) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, labelsize=9, length=0)
    if xgrid:
        ax.xaxis.grid(True, color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)


def _finish(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, facecolor=SURFACE,
                bbox_inches="tight", pad_inches=0.32)
    plt.close(fig)
    return buf.getvalue()


def _empty(message: str) -> bytes:
    fig, ax = plt.subplots(figsize=(7.5, 2.2))
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.axis("off")
    ax.text(0.5, 0.5, message, ha="center", va="center",
            fontsize=12, color=INK_MUTED)
    return _finish(fig)


# --------------------------------------------------------------------------- #
# portfolio
# --------------------------------------------------------------------------- #
def portfolio_chart(payload: dict[str, Any]) -> bytes:
    """Two panels sharing the holdings axis: what you own, and how it is doing.

    Kept side by side rather than merged, because allocation (a share of a whole)
    and P&L (a signed amount) are different measures and must not share a scale.
    """
    holdings = [h for h in payload.get("holdings", []) if h.get("current_value")]
    if not holdings:
        return _empty("No holdings yet — place a trade and this chart fills in.")

    holdings = sorted(holdings, key=lambda h: h.get("current_value") or 0)
    names = [h["trading_symbol"] for h in holdings]
    values = [h.get("current_value") or 0 for h in holdings]
    pnls = [h.get("pnl") or 0 for h in holdings]
    total = sum(values) or 1.0
    y = range(len(names))

    if len(names) == 1:
        # One holding is 100% of the portfolio by definition — a bar comparing it
        # to nothing is a slab of colour that says less than the number does.
        return _single_holding_tile(holdings[0], payload)

    # Thin the bars for short lists so three holdings do not render as slabs.
    bar = min(BAR_HEIGHT, 0.18 + 1.1 / len(names))
    height = max(2.8, 0.46 * len(names) + 1.8)
    fig, (ax_alloc, ax_pnl) = plt.subplots(
        1, 2, figsize=(10.2, height), sharey=True,
        gridspec_kw={"width_ratios": [1.15, 1], "wspace": 0.08},
    )
    fig.patch.set_facecolor(SURFACE)
    # Room for the headline above the panel titles, which otherwise collide.
    fig.subplots_adjust(top=0.78)

    # --- allocation: one series, one colour ---
    ax_alloc.barh(list(y), values, height=bar, color=SERIES_1)
    _style_axes(ax_alloc)
    ax_alloc.set_yticks(list(y))
    ax_alloc.set_yticklabels(names, fontsize=9.5, color=INK)
    ax_alloc.xaxis.set_major_formatter(_MONEY_AXIS)
    ax_alloc.set_title("Holdings by value", fontsize=11, color=INK,
                       loc="left", pad=12, fontweight="medium")
    span = max(values)
    for i, value in zip(y, values, strict=True):
        ax_alloc.text(value + span * 0.02, i, f"{value / total * 100:.0f}%",
                      va="center", fontsize=8.5, color=INK_MUTED)
    ax_alloc.set_xlim(0, span * 1.18)

    # --- P&L: status colours, every bar labelled with its signed value ---
    colors = [GAIN if p >= 0 else LOSS for p in pnls]
    ax_pnl.barh(list(y), pnls, height=bar, color=colors)
    _style_axes(ax_pnl)
    ax_pnl.axvline(0, color=INK_MUTED, linewidth=1)
    ax_pnl.xaxis.set_major_formatter(_MONEY_AXIS)
    ax_pnl.set_title("Unrealised P&L", fontsize=11, color=INK,
                     loc="left", pad=12, fontweight="medium")
    reach = max(max((abs(p) for p in pnls), default=0.0), 1.0)
    for i, pnl in zip(y, pnls, strict=True):
        offset = reach * 0.04
        ax_pnl.text(pnl + (offset if pnl >= 0 else -offset), i, _signed(pnl),
                    va="center", ha="left" if pnl >= 0 else "right",
                    fontsize=8.5, color=INK_MUTED)
    ax_pnl.set_xlim(-reach * 1.45, reach * 1.45)

    fig.suptitle(_headline(payload, len(names)), fontsize=12.5, color=INK,
                 x=0.01, ha="left", y=0.99, fontweight="semibold")
    return _finish(fig)


def _headline(payload: dict[str, Any], count: int) -> str:
    summary = payload.get("summary", {})
    held = summary.get("holdings_count", count)
    return (f"Portfolio  ·  {_rupees(summary.get('current_value', 0))} across "
            f"{held} holding{'' if held == 1 else 's'}  ·  "
            f"overall {_signed(summary.get('overall_pnl', 0))}")


def _single_holding_tile(holding: dict[str, Any], payload: dict[str, Any]) -> bytes:
    """The whole portfolio in one name — stated, not plotted."""
    pnl = holding.get("pnl") or 0
    pnl_pct = holding.get("pnl_pct")
    fig, ax = plt.subplots(figsize=(9.0, 2.5))
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.axis("off")

    ax.text(0, 0.93, _headline(payload, 1), fontsize=11.5, color=INK,
            fontweight="semibold")
    ax.text(0, 0.46, holding["trading_symbol"], fontsize=26, color=INK,
            fontweight="bold", va="center")
    ax.text(0, 0.08,
            f"{holding.get('quantity', 0)} shares  ·  avg "
            f"{_rupees(holding.get('average_price', 0), 2)}  ·  now "
            f"{_rupees(holding.get('last_price', 0), 2)}",
            fontsize=9.5, color=INK_MUTED)

    tone = _tone(pnl)
    ax.text(1.0, 0.46, _signed(pnl), fontsize=30, color=tone, ha="right",
            va="center", fontweight="bold")
    if pnl_pct is not None:
        ax.text(1.0, 0.08, f"{pnl_pct:+.2f}%  unrealised", fontsize=9.5,
                color=tone, ha="right")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    return _finish(fig)


# --------------------------------------------------------------------------- #
# performance
# --------------------------------------------------------------------------- #
def _supporting_stats(perf: dict[str, Any]) -> str:
    stats = perf.get("stats", {}) or {}
    parts = []
    if stats.get("win_rate") is not None:
        parts.append(f"{stats['win_rate']:.0f}% win rate")
    if stats.get("closed_trades") is not None:
        parts.append(f"{stats['closed_trades']} closed trades")
    unrealized = perf.get("unrealized")
    if unrealized is not None:
        parts.append(f"{_signed(unrealized)} still open")
    return "   ·   ".join(parts)


def _stat_tile(perf: dict[str, Any], items: list[tuple[str, float]]) -> bytes:
    """One or two closed positions is a number, not a bar chart.

    A single bar encodes nothing the number does not already say, and renders as
    a giant slab of colour.
    """
    booked = perf.get("total_realized", 0)
    fig, ax = plt.subplots(figsize=(8.6, 2.5))
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.axis("off")

    ax.text(0, 0.92, "B O O K E D   P & L", fontsize=9, color=INK_MUTED,
            fontweight="medium")
    ax.text(0, 0.46, _signed(booked), fontsize=40,
            color=_tone(booked), fontweight="bold", va="center")
    ax.text(0, 0.10, _supporting_stats(perf), fontsize=9.5, color=INK_MUTED)

    for offset, (name, value) in enumerate(items):
        ax.text(0.66, 0.62 - offset * 0.24, name, fontsize=10, color=INK)
        ax.text(1.0, 0.62 - offset * 0.24, _signed(value), fontsize=10,
                color=_tone(value), ha="right", fontweight="medium")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    return _finish(fig)


def performance_chart(perf: dict[str, Any]) -> bytes:
    """Booked P&L — the 'did I actually make money' picture."""
    if not perf.get("available"):
        return _empty("Booked-P&L analytics are available in paper mode.")

    by_symbol = perf.get("realized_by_symbol") or {}
    if not by_symbol:
        return _empty("No closed trades yet — booked P&L appears after you sell.")

    items = sorted(by_symbol.items(), key=lambda kv: kv[1])
    if len(items) <= 2:
        return _stat_tile(perf, list(reversed(items)))

    names = [k for k, _ in items]
    values = [v for _, v in items]
    y = range(len(names))

    # Cap the bar thickness so a short list does not render as slabs.
    bar = min(BAR_HEIGHT, 0.16 + 0.9 / len(names))
    height = max(2.6, 0.46 * len(names) + 1.6)
    fig, ax = plt.subplots(figsize=(8.6, height))
    fig.patch.set_facecolor(SURFACE)

    ax.barh(list(y), values, height=bar,
            color=[GAIN if v >= 0 else LOSS for v in values])
    _style_axes(ax)
    ax.axvline(0, color=INK_MUTED, linewidth=1)
    ax.set_yticks(list(y))
    ax.set_yticklabels(names, fontsize=9.5, color=INK)
    ax.xaxis.set_major_formatter(_MONEY_AXIS)

    reach = max((abs(v) for v in values), default=1) or 1
    for i, value in zip(y, values, strict=True):
        offset = reach * 0.04
        ax.text(value + (offset if value >= 0 else -offset), i, _signed(value),
                va="center", ha="left" if value >= 0 else "right",
                fontsize=8.5, color=INK_MUTED)
    ax.set_xlim(-reach * 1.4, reach * 1.4)

    ax.set_title(
        f"Booked P&L by stock  ·  {_signed(perf.get('total_realized', 0))}  ·  "
        + _supporting_stats(perf),
        fontsize=12, color=INK, loc="left", pad=14, fontweight="semibold",
    )
    return _finish(fig)


# --------------------------------------------------------------------------- #
# single-stock analysis
# --------------------------------------------------------------------------- #
def analysis_chart(analysis: dict[str, Any], history: dict[str, list]) -> bytes:
    """Price with its Bollinger envelope and risk levels, RSI stacked beneath.

    Two panels rather than two y-scales: price in rupees and RSI in 0-100 have no
    common scale, and overlaying them would imply a relationship the data has not
    got.
    """
    closes = history.get("close") or []
    if len(closes) < 5:
        return _empty("Not enough price history to chart.")

    dates = history.get("dates") or list(range(len(closes)))
    upper = history.get("bb_upper") or []
    lower = history.get("bb_lower") or []
    rsi = history.get("rsi") or []
    x = range(len(closes))

    fig, (ax_price, ax_rsi) = plt.subplots(
        2, 1, figsize=(10.2, 5.6), sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.12},
    )
    fig.patch.set_facecolor(SURFACE)

    # --- price + envelope + risk levels ---
    if len(upper) == len(closes) and len(lower) == len(closes):
        ax_price.fill_between(x, lower, upper, color=BAND, linewidth=0,
                              label="Bollinger band (20d, ±2σ)")
    ax_price.plot(x, closes, color=SERIES_1, linewidth=2, label="Close")

    risk = analysis.get("risk", {})
    for level, label, color in (
        (risk.get("stop_loss"), "Stop-loss", LOSS),
        (risk.get("target_1"), "Target 1", GAIN),
        (risk.get("target_2"), "Target 2", GAIN),
    ):
        if not level:
            continue
        ax_price.axhline(level, color=color, linewidth=1.1,
                         linestyle="--", alpha=0.75)
        # Masked with the surface colour so the dashed line does not run through
        # the text and read as a strikethrough.
        ax_price.text(len(closes) - 0.5, level, f" {label} {_rupees(level)} ",
                      va="center", fontsize=8, color=color,
                      bbox={"facecolor": SURFACE, "edgecolor": "none", "pad": 1.2})

    _style_axes(ax_price, xgrid=False)
    ax_price.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax_price.set_axisbelow(True)
    ax_price.yaxis.set_major_formatter(_MONEY_AXIS)
    ax_price.legend(loc="upper left", frameon=False, fontsize=8.5,
                    labelcolor=INK_MUTED, ncols=2)
    ax_price.set_xlim(0, len(closes) * 1.16)

    verdict = analysis.get("verdict", "")
    score = analysis.get("composite_score")
    tone = {"BUY": GAIN, "SELL": LOSS}.get(verdict, INK_MUTED)
    ax_price.set_title(
        f"{analysis.get('symbol', '')}  ·  {_rupees(analysis.get('price', 0), 2)}",
        fontsize=12.5, color=INK, loc="left", pad=14, fontweight="semibold",
    )
    ax_price.text(1.0, 1.045, f"{verdict}  {score}/100",
                  transform=ax_price.transAxes, ha="right", va="bottom",
                  fontsize=11.5, color=tone, fontweight="bold")

    # --- RSI, its own scale ---
    if len(rsi) == len(closes):
        ax_rsi.plot(x, rsi, color=SERIES_1, linewidth=1.6)
        ax_rsi.axhspan(30, 70, color=GRID, alpha=0.45, linewidth=0)
        for level, label in ((70, "overbought"), (30, "oversold")):
            ax_rsi.axhline(level, color=NEUTRAL, linewidth=1, linestyle=":")
            ax_rsi.text(len(closes) - 0.5, level, f" {label}", va="center",
                        fontsize=7.5, color=INK_MUTED)
        ax_rsi.set_ylim(0, 100)
        ax_rsi.set_yticks([0, 30, 70, 100])
    _style_axes(ax_rsi, xgrid=False)
    ax_rsi.set_ylabel("RSI-14", fontsize=9, color=INK_MUTED)
    ax_rsi.set_xlim(0, len(closes) * 1.16)

    # Sparse date ticks — a label per bar would be noise.
    if dates and isinstance(dates[0], str):
        step = max(1, len(dates) // 6)
        ticks = list(range(0, len(dates), step))
        ax_rsi.set_xticks(ticks)
        ax_rsi.set_xticklabels([dates[i] for i in ticks], fontsize=8.5)
    return _finish(fig)


# --------------------------------------------------------------------------- #
# welcome card
# --------------------------------------------------------------------------- #
CARD_BG = "#f4f6fa"      # a hair cooler than the chart surface, so it reads as a card
ACCENT = "#1c5cab"


def _capability(ax, x: float, width: float, title: str, lines: list[str]) -> None:
    """One capability panel: a rule, a title, and two supporting lines."""
    ax.add_patch(plt.Rectangle((x, 0.30), width, 0.30, transform=ax.transAxes,
                               facecolor=SURFACE, edgecolor=GRID, linewidth=1,
                               zorder=1))
    ax.plot([x + 0.022, x + 0.075], [0.545, 0.545], transform=ax.transAxes,
            color=ACCENT, linewidth=2.4, solid_capstyle="round", zorder=2)
    ax.text(x + 0.022, 0.485, title, transform=ax.transAxes, fontsize=10.5,
            color=INK, fontweight="bold", zorder=2)
    for offset, line in enumerate(lines):
        ax.text(x + 0.022, 0.415 - offset * 0.052, line, transform=ax.transAxes,
                fontsize=8.3, color=INK_MUTED, zorder=2)


def welcome_card(mode: str = "paper", available_cash: float | None = None,
                 broker: str | None = None) -> bytes:
    """The first thing a new user sees.

    Rendered rather than described, because the opening impression of a product
    that will one day place real orders should look like it was built with care.
    Personalised with the user's own mode and balance so it reads as *their*
    account, not a brochure.
    """
    fig, ax = plt.subplots(figsize=(10.4, 4.5))
    fig.patch.set_facecolor(CARD_BG)
    ax.set_facecolor(CARD_BG)
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    ax.text(0.022, 0.90, "TRINETRA CAPITAL AI", fontsize=21, color=INK,
            fontweight="bold", transform=ax.transAxes)
    ax.text(0.022, 0.795, "Agentic research and execution for Indian equities · NSE & BSE",
            fontsize=10.5, color=INK_MUTED, transform=ax.transAxes)
    ax.plot([0.022, 0.978], [0.735, 0.735], transform=ax.transAxes,
            color=GRID, linewidth=1)

    gap, width = 0.018, 0.3053
    _capability(ax, 0.022, width, "RESEARCH", [
        "Live quotes and fundamentals", "Exact symbol resolution"])
    _capability(ax, 0.022 + width + gap, width, "ANALYSE", [
        "RSI · MACD · Bollinger · ATR", "Every step of the reasoning shown"])
    _capability(ax, 0.022 + 2 * (width + gap), width, "EXECUTE", [
        "Preview, then your approval", "Paper now, your broker when ready"])

    live = mode == "live"
    badge = "LIVE — REAL MONEY" if live else "PAPER MODE"
    tone = LOSS if live else GAIN
    ax.add_patch(plt.Rectangle((0.022, 0.135), 0.956, 0.115, transform=ax.transAxes,
                               facecolor=SURFACE, edgecolor=GRID, linewidth=1))
    ax.text(0.042, 0.192, badge, fontsize=10, color=tone, fontweight="bold",
            va="center", transform=ax.transAxes)
    detail = f"{_rupees(available_cash)} available" if available_cash is not None else ""
    if not live:
        detail += "  ·  simulated money, real live prices" if detail else ""
    if broker:
        detail += f"  ·  {broker}"
    ax.text(0.235, 0.192, detail, fontsize=9.4, color=INK_MUTED, va="center",
            transform=ax.transAxes)

    ax.text(0.022, 0.045,
            "Guardrails:  per-order cap  ·  daily limits  ·  instant kill switch  ·  "
            "broker keys never typed in chat",
            fontsize=8.6, color=INK_MUTED, transform=ax.transAxes)
    return _finish(fig)
