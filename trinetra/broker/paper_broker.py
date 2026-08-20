"""Simulated broker (paper trading).

Fills are instant and logged to portfolio.json (backward compatible with the
original flat trade-log format). Holdings, positions and a virtual cash balance
are derived by aggregating that log, and enriched with live LTP from the market
data layer so paper P&L still tracks the real market.

This is the default broker so users can exercise the full agent system safely
before flipping GROWW_TRADING_MODE=live.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from trinetra.broker.base import (
    Broker,
    BrokerError,
    Funds,
    Holding,
    OrderRequest,
    OrderResult,
    Position,
)
from trinetra.logging_setup import get_logger
from trinetra.store import get_store

log = get_logger(__name__)


class PaperBroker(Broker):
    name = "paper"
    mode = "paper"

    # ------------------------------------------------------------------ #
    # trade-log persistence
    # ------------------------------------------------------------------ #
    def _load(self) -> list[dict[str, Any]]:
        return get_store(self.ctx).load_trades()

    def _save(self, trades: list[dict[str, Any]]) -> None:
        get_store(self.ctx).save_trades(trades)

    # ------------------------------------------------------------------ #
    # orders
    # ------------------------------------------------------------------ #
    def _preflight(self, req: OrderRequest, reference_price: float | None = None) -> float:
        """Every check that can be made without mutating state. Returns the fill
        price. Shared by `validate_order` (preview) and `place_order` (execution)
        so a preview can never approve something execution would then reject."""
        # Paper mode has no live feed to watch a stop trigger, so be honest.
        if req.order_type in ("SL", "SL_M"):
            raise BrokerError(
                "Stop-loss (SL/SL_M) orders aren't simulated in paper mode (there is no "
                "live trigger monitoring). Switch to live mode, or use a market/limit order."
            )

        # A market order needs a fill price; a limit order fills at its limit.
        fill_price = req.price if (req.order_type == "LIMIT" and req.price) else reference_price
        if not fill_price or fill_price <= 0:
            raise BrokerError(
                "Paper market order needs a reference price. Have the research agent "
                "fetch a live quote first, or place a LIMIT order with an explicit price."
            )

        self.guard_order(req, fill_price)

        # Mirror real-account constraints so paper trading surfaces the same
        # affordability/position errors you'd hit live.
        agg = self._aggregate()
        held = int(agg.get(req.trading_symbol, {}).get("qty", 0))
        if req.transaction_type == "SELL" and req.quantity > held:
            raise BrokerError(
                f"Cannot sell {req.quantity} {req.trading_symbol}: the paper "
                f"portfolio holds only {held} share(s)."
            )
        total = round(req.quantity * fill_price, 2)
        if req.transaction_type == "BUY":
            net_invested = 0.0
            for t in self._load():
                a = str(t.get("action", "")).lower()
                tot = float(t.get("total", 0) or 0)
                net_invested += tot if a == "buy" else (-tot if a == "sell" else 0.0)
            available = round(self.ctx.paper_starting_cash - net_invested, 2)
            if total > available:
                raise BrokerError(
                    f"Insufficient paper cash: this order costs ₹{total:,.2f} but "
                    f"only ₹{available:,.2f} is available. Reduce the quantity or "
                    "sell holdings first."
                )
        return fill_price

    def validate_order(self, req: OrderRequest, reference_price: float | None = None) -> None:
        self._preflight(req.normalised(), reference_price)

    def place_order(self, req: OrderRequest, reference_price: float | None = None) -> OrderResult:
        req = req.normalised()
        fill_price = self._preflight(req, reference_price)
        total = round(req.quantity * fill_price, 2)

        order_id = f"PAPER-{req.reference_id}"
        trades = self._load()
        trades.append(
            {
                "timestamp": datetime.now().isoformat(),
                "symbol": req.trading_symbol,
                "exchange": req.exchange,
                "action": req.transaction_type.lower(),
                "product": req.product,
                "currency": "INR",
                "shares": req.quantity,
                "price": round(fill_price, 2),
                "total": total,
                "order_id": order_id,
                "reference_id": req.reference_id,
                "mode": "paper",
            }
        )
        self._save(trades)
        log.info(
            "PAPER fill → %s %s x%d @ ₹%.2f (₹%.2f)",
            req.transaction_type, req.trading_symbol, req.quantity, fill_price, total,
        )

        return OrderResult(
            status="filled",
            transaction_type=req.transaction_type,
            trading_symbol=req.trading_symbol,
            quantity=req.quantity,
            order_type=req.order_type,
            product=req.product,
            mode=self.mode,
            order_id=order_id,
            price=round(fill_price, 2),
            average_price=round(fill_price, 2),
            estimated_value=total,
            reference_id=req.reference_id,
            exchange=req.exchange,
            message="Simulated fill (paper trading).",
        )

    def cancel_order(self, order_id: str, segment: str = "CASH") -> dict[str, Any]:
        # Paper orders fill instantly, so there is nothing pending to cancel.
        return {
            "order_id": order_id,
            "status": "not_cancellable",
            "message": "Paper orders fill immediately; nothing to cancel.",
        }

    def modify_order(
        self,
        order_id: str,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
        order_type: str | None = None,
        segment: str = "CASH",
    ) -> dict[str, Any]:
        return {
            "order_id": order_id,
            "status": "not_modifiable",
            "message": "Paper orders fill immediately; nothing to modify. "
                       "Place a new order instead.",
        }

    def get_order_status(self, order_id: str, segment: str = "CASH") -> dict[str, Any]:
        for t in self._load():
            if t.get("order_id") == order_id:
                return {"order_id": order_id, "status": "filled", **t}
        return {"order_id": order_id, "status": "unknown"}

    def get_order_history(self, limit: int = 20, segment: str = "CASH") -> list[dict[str, Any]]:
        # Most-recent first.
        trades = self._load()
        return list(reversed(trades))[:limit]

    # ------------------------------------------------------------------ #
    # portfolio (derived from the trade log)
    # ------------------------------------------------------------------ #
    def _analyze(self):
        """Single source of truth for cost basis + realized P&L (see analytics)."""
        from trinetra import analytics  # lazy: keeps analytics broker-free

        return analytics.analyze(self._load())

    def _aggregate(self) -> dict[str, dict[str, float]]:
        """symbol -> {qty, buy_qty, buy_cost, realized} across all trades.

        Kept as a thin adapter over analytics so callers that only need the
        current quantity / cost basis (e.g. the oversell check) are unaffected.
        """
        return {
            sym: {"qty": st.qty, "buy_qty": st.buy_qty,
                  "buy_cost": st.buy_cost, "realized": st.realized}
            for sym, st in self._analyze().per_symbol.items()
        }

    def get_holdings(self) -> list[Holding]:
        from trinetra import analytics, market_data  # lazy to avoid import cycle

        analysis = self._analyze()
        states = analysis.per_symbol
        held = [sym for sym, st in states.items() if int(st.qty) > 0]
        prices = market_data.ltp_many(held) if held else {}
        now = datetime.now()

        holdings: list[Holding] = []
        for sym, st in states.items():
            qty = int(st.qty)
            if qty <= 0:
                continue
            avg = round(st.avg_cost, 2)
            last = prices.get(sym)
            invested = round(qty * avg, 2)
            cur = round(qty * last, 2) if last else None
            pnl = round(cur - invested, 2) if cur is not None else None
            pnl_pct = round((pnl / invested) * 100, 2) if (pnl is not None and invested) else None
            holding_days = None
            first_dt = analytics.parse_ts(st.first_buy_ts)
            if first_dt is not None:
                holding_days = max((now - first_dt).days, 0)
            holdings.append(
                Holding(
                    trading_symbol=sym,
                    quantity=qty,
                    average_price=avg,
                    last_price=last,
                    invested=invested,
                    current_value=cur,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    realised_pnl=round(st.realized, 2) if abs(st.realized) > 1e-9 else None,
                    holding_days=holding_days,
                )
            )
        return holdings

    def realized_total(self) -> float | None:
        return self._analyze().total_realized

    def performance(self) -> dict[str, Any]:
        """Booked (realized) P&L, trade stats, best/worst, closed positions and
        today's P&L — all derived from the paper trade log."""
        from trinetra import market_data  # lazy

        analysis = self._analyze()
        stats = analysis.stats()
        best = analysis.best_trade()
        worst = analysis.worst_trade()

        # Unrealized on still-open holdings, so we can show an overall figure.
        holdings = self.get_holdings()
        unrealized = round(sum(h.pnl or 0.0 for h in holdings), 2)

        # Today's P&L = booked-today + today's mark-to-market on open holdings.
        today_realized = analysis.realized_today(date.today())
        today_unreal = 0.0
        for h in holdings:
            try:
                q = market_data.get_live_quote(h.trading_symbol)
                dc = q.get("day_change")
                if dc is not None:
                    today_unreal += float(dc) * h.quantity
            except Exception as exc:  # noqa: BLE001 - best effort, never fatal
                log.debug("day-change fetch failed for %s: %s", h.trading_symbol, exc)
        today_total = round(today_realized + today_unreal, 2)

        def _event(e):
            if e is None:
                return None
            return {"symbol": e.symbol, "shares": int(e.shares), "realized": e.realized,
                    "realized_pct": e.realized_pct, "sell_price": e.sell_price,
                    "avg_cost": e.avg_cost, "timestamp": e.timestamp}

        return {
            "available": True,
            "mode": self.mode,
            "total_realized": analysis.total_realized,
            "realized_by_symbol": analysis.realized_by_symbol(),
            "unrealized": unrealized,
            "overall_pnl": round(analysis.total_realized + unrealized, 2),
            "stats": stats,
            "best_trade": _event(best),
            # Suppress a redundant 'worst' when there's a single trade (best is worst).
            "worst_trade": _event(worst) if worst is not best else None,
            "closed_positions": analysis.closed_positions(),
            "today": {"realized": today_realized,
                      "unrealized": round(today_unreal, 2),
                      "total": today_total},
        }

    def get_positions(self, segment: str | None = None) -> list[Position]:
        # Paper trading models everything as holdings; no intraday position book.
        return [
            Position(
                trading_symbol=h.trading_symbol,
                quantity=h.quantity,
                product="CNC",
                segment="CASH",
                average_price=h.average_price,
                last_price=h.last_price,
                unrealised_pnl=h.pnl,
            )
            for h in self.get_holdings()
        ]

    def get_funds(self) -> Funds:
        net_invested = 0.0
        for t in self._load():
            action = str(t.get("action", "")).lower()
            total = float(t.get("total", 0) or 0)
            if action == "buy":
                net_invested += total
            elif action == "sell":
                net_invested -= total
        available = self.ctx.paper_starting_cash - net_invested

        # Net worth = free cash + current market value of holdings. Falls back to
        # cost basis if live prices are unavailable, and never lets a market-data
        # hiccup break the funds read.
        try:
            holdings_value = sum(
                (h.current_value if h.current_value is not None else (h.invested or 0.0))
                for h in self.get_holdings()
            )
        except Exception as exc:  # noqa: BLE001 - funds must never hard-fail
            log.debug("Could not value paper holdings for net worth: %s", exc)
            holdings_value = max(net_invested, 0.0)

        return Funds(
            available_cash=round(available, 2),
            margin_used=round(net_invested, 2),
            net=round(available + holdings_value, 2),
            mode=self.mode,
            detail={"starting_cash": self.ctx.paper_starting_cash},
        )
