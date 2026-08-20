"""LangChain tools exposed to the CLI agents.

Thin adapters only. Every one delegates to `trinetra.services`, which is the same
layer the MCP server calls — so the CLI and the MCP product can never drift apart
in behaviour.

Risky tools (`place_order`, `cancel_order`, `modify_order`) are listed in
RISKY_TOOLS and gated by the Human-in-the-Loop middleware in agents.py. By the
time one of them executes here, the user has already approved it at the CLI
prompt, so the order runs in a single step; the MCP server, which has no such
prompt, uses the two-step preview/confirm flow instead.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from trinetra.broker import get_broker
from trinetra.broker.base import BrokerError
from trinetra.session import default_context
from trinetra.logging_setup import get_logger
from trinetra.services import research as research_svc
from trinetra.services import trading as trading_svc

log = get_logger(__name__)


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


# --------------------------------------------------------------------------- #
# Research tools
# --------------------------------------------------------------------------- #
@tool("lookup_stocks")
def lookup_stocks(company_name: str) -> str:
    """Resolve a company name to its tradable stock symbol on NSE/BSE.

    Use this first whenever the user names a company (e.g. "Reliance", "TCS NSE",
    "Infosys BSE") so later tools get a precise Groww trading symbol. Returns the
    trading_symbol, exchange and resolved company name.
    """
    return _json(research_svc.lookup_stocks(company_name))


@tool("get_live_quote")
def get_live_quote(symbol: str) -> str:
    """Get the real-time market quote for a stock (Groww live feed when connected,
    else yfinance). Returns last price, day change, day high/low, open, previous
    close, volume and 52-week range. Use this to know the current price before
    advising on or placing an order. `symbol` may be "RELIANCE", "RELIANCE.NS" or
    "TCS".
    """
    return _json(research_svc.get_quote(symbol))


@tool("fetch_stock_data")
def fetch_stock_data(symbol: str) -> str:
    """Fetch a combined snapshot for a stock: live price/day-change plus
    fundamentals (company name, sector, market cap, P/E, 52-week high/low).
    Use for "tell me about X" / research questions.
    """
    return _json(research_svc.get_stock_snapshot(symbol))


@tool("analyze_stock_sentiment")
def analyze_stock_sentiment(symbol: str) -> str:
    """Run technical + news-sentiment analysis on a stock and return a BUY/SELL/HOLD
    signal. Computes RSI-14, MACD, Bollinger %B and ATR from 90 days of history,
    scores recent headlines, and derives a composite score with ATR-based
    stop-loss and targets. The `reasoning` array explains how the verdict was
    reached — relay it rather than inventing your own rationale. Use when the user
    asks "should I buy X?" or "what's the outlook for X?".
    """
    return _json(research_svc.analyze_stock(symbol))


# --------------------------------------------------------------------------- #
# Trading tools
# --------------------------------------------------------------------------- #
@tool("place_order")
def place_order(
    symbol: str,
    action: str,
    quantity: int,
    order_type: str = "market",
    price: float = 0.0,
    trigger_price: float = 0.0,
    product: str = "",
    exchange: str = "",
) -> str:
    """Place a buy or sell equity order through the connected broker.

    Parameters:
    - symbol: stock trading symbol (e.g. "RELIANCE", "TCS"). ".NS"/".BO" suffixes are accepted.
    - action: "buy" or "sell".
    - quantity: number of shares (whole number, > 0). For budget-based orders,
      compute floor(budget / price) yourself and pass that here.
    - order_type: "market" (fill at current price), "limit" (needs price),
      "sl" (stop-loss limit: needs price + trigger_price), or
      "sl_m" (stop-loss market: needs trigger_price).
    - price: limit price per share — required for limit and sl orders.
    - trigger_price: trigger price — required for sl and sl_m (stop-loss) orders.
    - product: "CNC" (delivery) or "MIS" (intraday). Defaults to the configured product.
    - exchange: "NSE" or "BSE". Defaults to the configured exchange.

    In PAPER mode the fill is simulated and logged; in LIVE mode it is sent to the
    real Groww account (and gated by human approval first). Stop-loss orders are
    LIVE-only. Returns the order result as JSON.
    """
    ctx = default_context()
    preview = trading_svc.preview_order(
        ctx, symbol, action, quantity, order_type=order_type, price=price,
        trigger_price=trigger_price, product=product, exchange=exchange,
    )
    if preview.get("status") != "preview":
        return _json(preview)

    result = trading_svc.execute_order(ctx, preview["order"])
    detail = preview["preview"]
    if "resolved_from" in detail:
        result["note"] = f"Resolved {detail['resolved_from']}."
    result["resolved_name"] = detail.get("company")
    return _json(result)


@tool("modify_order")
def modify_order(
    order_id: str,
    quantity: int = 0,
    price: float = 0.0,
    trigger_price: float = 0.0,
    segment: str = "CASH",
) -> str:
    """Modify a pending (not-yet-filled) LIVE order: change its quantity, limit
    price, or trigger price. Pass only the fields you want to change (leave others
    at 0). Returns the result as JSON. Not applicable in paper mode (orders fill
    instantly).
    """
    try:
        return _json(
            get_broker().modify_order(
                order_id,
                quantity=quantity or None,
                price=price or None,
                trigger_price=trigger_price or None,
                segment=segment,
            )
        )
    except BrokerError as exc:
        return _json({"status": "error", "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        log.exception("modify_order failed")
        return _json({"status": "error", "error": str(exc)})


@tool("get_order_history")
def get_order_history(limit: int = 20) -> str:
    """Show recent orders (the order book): symbol, side, quantity, price, type and
    status. Use when the user asks "what did I trade today?", "show my orders", or
    "order history". Returns a ready-to-display markdown table.
    """
    return _json(trading_svc.get_order_history(default_context(), limit=limit))


@tool("cancel_order")
def cancel_order(order_id: str, segment: str = "CASH") -> str:
    """Cancel a previously placed (pending) order by its broker order id.
    Only meaningful in LIVE mode for orders that have not yet filled. Returns the
    cancellation result as JSON.
    """
    try:
        return _json(get_broker().cancel_order(order_id, segment=segment))
    except BrokerError as exc:
        return _json({"status": "error", "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        log.exception("cancel_order failed")
        return _json({"status": "error", "error": str(exc)})


@tool("get_order_status")
def get_order_status(order_id: str, segment: str = "CASH") -> str:
    """Look up the current status of an order by its broker order id (e.g. to check
    if a live order filled). Returns the status payload as JSON.
    """
    try:
        return _json(get_broker().get_order_status(order_id, segment=segment))
    except BrokerError as exc:
        return _json({"status": "error", "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        log.exception("get_order_status failed")
        return _json({"status": "error", "error": str(exc)})


@tool("view_portfolio")
def view_portfolio() -> str:
    """View the current portfolio: holdings (symbol, quantity, average price, live
    price, P&L) and open positions. Reads real data from Groww in LIVE mode, or the
    simulated portfolio in PAPER mode. Call this whenever the user asks to see their
    portfolio, holdings, or P&L — do not rely on conversation history.
    """
    return _json(trading_svc.view_portfolio(default_context()))


@tool("get_funds")
def get_funds() -> str:
    """Get available trading funds / buying power. In LIVE mode this returns the
    real Groww margin (available cash, margin used); in PAPER mode it returns the
    simulated cash balance. Use before placing an order to confirm affordability.
    """
    return _json(trading_svc.get_funds(default_context()))


@tool("get_performance")
def get_performance() -> str:
    """Show TRADING PERFORMANCE and booked P&L: total realized (booked) profit/loss
    to date, per-stock realized P&L, win rate, best & worst trades, fully-closed
    positions, today's P&L, and sector allocation. Use whenever the user asks
    "how much profit/loss have I booked?", "am I net positive overall?", "show my
    stats / performance / track record", "realized P&L", or "which trades won/lost".
    Returns a ready-to-display report. Paper mode only (live has no historical
    booked-P&L source).
    """
    return _json(trading_svc.get_performance(default_context()))


# Tools whose execution must be approved by a human before running.
RISKY_TOOLS = {"place_order", "cancel_order", "modify_order"}

RESEARCH_TOOLS = [lookup_stocks, get_live_quote, fetch_stock_data]
SENTIMENT_TOOLS = [analyze_stock_sentiment]
TRADING_TOOLS = [
    place_order,
    cancel_order,
    modify_order,
    get_order_status,
    get_order_history,
    view_portfolio,
    get_funds,
    get_performance,
]
