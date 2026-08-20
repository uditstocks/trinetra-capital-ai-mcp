"""Account and order services.

Account-scoped: every function takes a SessionContext and reads through the
broker bound to it, so two users can never see each other's positions.

Orders are two steps. `preview_order` resolves the symbol, prices the order and
runs every broker pre-flight check without touching the account; `execute_order`
runs an instruction that already passed that gate. The caller holds the validated
instruction between the two, so nothing can alter what gets placed in between.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any

from trinetra import instruments, market_data, render, session
from trinetra.broker import get_broker
from trinetra.broker.base import BrokerError, OrderRequest
from trinetra.logging_setup import get_logger
from trinetra.session import SessionContext

log = get_logger(__name__)

CONCENTRATION_PCT = 25.0
# Order types with no explicit price, so they need a live reference price.
_NEEDS_REFERENCE = ("market", "sl_m")


# --------------------------------------------------------------------------- #
# account views
# --------------------------------------------------------------------------- #
def view_portfolio(ctx: SessionContext) -> dict[str, Any]:
    """Holdings, positions, funds and P&L for this account."""
    try:
        broker = get_broker(ctx)
        holdings = [h.to_dict() for h in broker.get_holdings()]
        positions = [p.to_dict() for p in broker.get_positions()]
        funds = broker.get_funds().to_dict()

        total_pnl = round(sum(h.get("pnl", 0) or 0 for h in holdings), 2)
        total_value = round(sum(h.get("current_value", 0) or 0 for h in holdings), 2)
        total_invested = round(sum(h.get("invested", 0) or 0 for h in holdings), 2)

        # Booked P&L across ALL symbols, including fully-closed ones absent from
        # holdings. None in live mode -> fall back to what the holdings carry.
        total_realized = broker.realized_total()
        if total_realized is None:
            total_realized = round(sum(h.get("realised_pnl", 0) or 0 for h in holdings), 2)

        overweight: list[str] = []
        for h in holdings:
            current = h.get("current_value")
            if current is not None and total_value:
                pct = round(current / total_value * 100, 1)
                h["allocation_pct"] = pct
                if pct > CONCENTRATION_PCT:
                    overweight.append(f"{h['trading_symbol']} ({pct:.0f}%)")

        payload = {
            "mode": broker.mode,
            "holdings": holdings,
            "positions": positions,
            "funds": funds,
            "summary": {
                "total_invested": total_invested,
                "current_value": total_value,
                "total_pnl": total_pnl,           # unrealized (mark-to-market)
                "realized_pnl": total_realized,   # booked on shares already sold
                "overall_pnl": round(total_pnl + total_realized, 2),
                "holdings_count": len(holdings),
                "concentration_alert": overweight or None,
            },
        }
        payload["display"] = render.render_portfolio(payload)
        return payload
    except BrokerError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.exception("view_portfolio failed")
        return {"error": str(exc)}


def get_funds(ctx: SessionContext) -> dict[str, Any]:
    """Available buying power: free cash, margin used, net worth."""
    try:
        return get_broker(ctx).get_funds().to_dict()
    except BrokerError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.exception("get_funds failed")
        return {"error": str(exc)}


def get_order_history(ctx: SessionContext, limit: int = 20) -> dict[str, Any]:
    """Recent orders (the order book), most recent first."""
    try:
        broker = get_broker(ctx)
        orders = broker.get_order_history(limit=limit)
        return {
            "mode": broker.mode,
            "count": len(orders),
            "display": render.render_orders(orders, broker.mode),
            "orders": orders,
        }
    except BrokerError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.exception("get_order_history failed")
        return {"error": str(exc)}


def _sector_breakdown(holdings: list) -> list[dict[str, Any]]:
    """Group holdings' market value by sector. Best-effort via yfinance, fetched
    concurrently; anything unclassifiable lands in 'Unknown'."""
    priced = [h for h in holdings if getattr(h, "current_value", None)]
    if not priced:
        return []

    def sector_of(holding) -> str:
        try:
            return market_data.fetch_fundamentals(
                holding.trading_symbol).get("sector") or "Unknown"
        except Exception:  # noqa: BLE001 - sector is decorative, never fatal
            return "Unknown"

    with ThreadPoolExecutor(max_workers=min(8, len(priced))) as pool:
        sectors = list(pool.map(sector_of, priced))

    total = sum(h.current_value for h in priced)
    aggregated: dict[str, float] = {}
    for holding, sector in zip(priced, sectors, strict=True):
        aggregated[sector] = aggregated.get(sector, 0.0) + holding.current_value

    out = [{"sector": s, "value": round(v, 2),
            "pct": round(v / total * 100, 1) if total else None}
           for s, v in aggregated.items()]
    out.sort(key=lambda d: d["value"], reverse=True)
    return out


def get_performance(ctx: SessionContext) -> dict[str, Any]:
    """Booked P&L, win rate, best/worst trades, sector allocation."""
    try:
        broker = get_broker(ctx)
        perf = broker.performance()
        if perf.get("available"):
            perf["sector_breakdown"] = _sector_breakdown(broker.get_holdings())
        perf["display"] = render.render_performance(perf)
        return perf
    except BrokerError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.exception("get_performance failed")
        return {"error": str(exc)}


# --------------------------------------------------------------------------- #
# orders
# --------------------------------------------------------------------------- #
def preview_order(
    ctx: SessionContext,
    symbol: str,
    action: str,
    quantity: int,
    order_type: str = "market",
    price: float = 0.0,
    trigger_price: float = 0.0,
    product: str = "",
    exchange: str = "",
) -> dict[str, Any]:
    """Resolve, price and risk-check an order without executing it.

    Returns `{"status": "rejected", ...}` or `{"status": "preview", "preview":
    {...}, "order": {...}}`. The `order` block is the validated instruction — the
    caller must retain it server-side and never expose it.
    """
    try:
        # Resolve against the instrument master first, so a wrong guess
        # ("INFOSYS") can never reach the broker — it becomes "INFY".
        rec = instruments.resolve(symbol, exchange or None)
        if rec is None:
            return {
                "status": "rejected",
                "error": f"Could not find a tradable symbol for {symbol!r}. "
                         "Use lookup_stocks to find the correct one.",
                "suggestions": [m.to_dict() for m in instruments.search(symbol, limit=3)],
            }

        side = action.strip().lower()
        if side not in ("buy", "sell"):
            return {"status": "rejected",
                    "error": f"action must be 'buy' or 'sell', got {action!r}."}
        if side == "buy" and not rec.buy_allowed:
            return {"status": "rejected",
                    "error": f"{rec.trading_symbol} is not buy-enabled on this exchange."}

        req = OrderRequest(
            trading_symbol=rec.trading_symbol,
            transaction_type=side,
            quantity=quantity,
            order_type=order_type or "market",
            price=price or 0.0,
            trigger_price=(trigger_price or None),
            product=(product or ctx.default_product),
            exchange=rec.exchange,
        ).normalised()

        reference_price = None
        if (order_type or "market").lower() in _NEEDS_REFERENCE:
            reference_price = market_data.try_ltp(rec.trading_symbol)

        broker = get_broker(ctx)
        broker.validate_order(req, reference_price)

        estimated_price = (
            req.price if req.order_type in ("LIMIT", "SL") and req.price
            else req.trigger_price if req.order_type == "SL_M" and req.trigger_price
            else reference_price
        )
        estimated_value = req.estimated_value(reference_price)

        warnings: list[str] = []
        if req.order_type == "MARKET":
            warnings.append("Market order — the actual fill price may differ from "
                            "this estimate.")
        if estimated_value and estimated_value > ctx.max_order_value * 0.8:
            warnings.append(
                f"This uses {estimated_value / ctx.max_order_value:.0%} of your "
                f"₹{ctx.max_order_value:,.0f} per-order safety cap."
            )

        preview = {
            "action": req.transaction_type,
            "symbol": req.trading_symbol,
            "company": rec.name,
            "exchange": req.exchange,
            "quantity": req.quantity,
            "order_type": req.order_type,
            "product": req.product,
            "estimated_price": round(estimated_price, 2) if estimated_price else None,
            "estimated_value": estimated_value,
            "mode": broker.mode,
            "is_real_money": ctx.is_live,
            "warnings": warnings,
        }
        if req.trigger_price:
            preview["trigger_price"] = req.trigger_price
        bare = symbol.strip().upper().removesuffix(".NS").removesuffix(".BO")
        if rec.trading_symbol != bare:
            preview["resolved_from"] = f"'{symbol}' → {rec.trading_symbol} ({rec.name})"

        session.audit_order(ctx, "preview", {"preview": preview})
        return {
            "status": "preview",
            "preview": preview,
            "order": {"request": asdict(req), "reference_price": reference_price},
        }
    except BrokerError as exc:
        return {"status": "rejected", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.exception("preview_order failed")
        return {"status": "rejected", "error": str(exc)}


def execute_order(ctx: SessionContext, order: dict[str, Any]) -> dict[str, Any]:
    """Place an order that already passed `preview_order`."""
    try:
        req = OrderRequest(**order["request"])
        reference_price = order.get("reference_price")
    except (KeyError, TypeError) as exc:
        return {"status": "rejected", "error": f"Malformed order instruction: {exc}"}

    session.audit_order(ctx, "submit", {
        "symbol": req.trading_symbol,
        "action": req.transaction_type,
        "quantity": req.quantity,
        "order_type": req.order_type,
        "reference_id": req.reference_id,
    })

    try:
        result = get_broker(ctx).place_order(req, reference_price=reference_price)
    except BrokerError as exc:
        session.audit_order(ctx, "rejected",
                            {"reference_id": req.reference_id, "error": str(exc)})
        return {"status": "rejected", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.exception("execute_order failed")
        session.audit_order(ctx, "failed",
                            {"reference_id": req.reference_id, "error": str(exc)})
        return {"status": "failed", "error": str(exc)}

    payload = result.to_dict()
    payload["trading_mode"] = ctx.trading_mode.value
    session.audit_order(ctx, "filled", payload)
    return payload
