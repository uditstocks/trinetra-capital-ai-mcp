"""Live broker backed by the Groww Trading API (equity cash segment, v1).

Maps the app's normalised order vocabulary onto the Groww SDK, normalises the
responses back into our dataclasses, and enriches holdings/positions with live
LTP so the agents can report real P&L. Any 401 triggers a single transparent
re-authentication retry.
"""

from __future__ import annotations

from collections.abc import Callable
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
from trinetra.broker import groww_client
from trinetra.logging_setup import get_logger
from trinetra.symbols import normalize

log = get_logger(__name__)


def _is_auth_error(exc: Exception) -> bool:
    """True only for genuine auth/session failures — narrow enough that an
    unrelated error whose text merely contains '401' won't trigger a re-auth."""
    name = type(exc).__name__.lower()
    if "authentication" in name or "authorisation" in name or "authorization" in name:
        return True
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if code == 401:
        return True
    msg = str(exc).lower()
    return "401" in msg and any(
        kw in msg for kw in ("unauth", "token", "expired", "session", "forbidden")
    )


class GrowwBroker(Broker):
    name = "groww"
    mode = "live"

    def __init__(self, ctx=None, client=None) -> None:
        super().__init__(ctx)
        # A client passed in belongs to ONE user, built from their own vaulted
        # credentials. A client we build ourselves comes from process
        # configuration and belongs to whoever runs the process — the CLI case.
        # The two must never be confused: see _call.
        self._user_scoped = client is not None
        self._client = client if client is not None else groww_client.get_client()

    # ------------------------------------------------------------------ #
    # low-level call wrapper with one auth-refresh retry
    # ------------------------------------------------------------------ #
    def _call(self, fn_name: str, **kwargs) -> dict[str, Any]:
        def invoke(client) -> dict[str, Any]:
            fn: Callable = getattr(client, fn_name)
            return fn(**kwargs)

        try:
            return invoke(self._client)
        except Exception as exc:  # noqa: BLE001
            if not _is_auth_error(exc):
                raise BrokerError(f"Groww {fn_name} failed: {exc}") from exc

            if self._user_scoped:
                # NEVER re-authenticate from process configuration here. Those are
                # the operator's credentials, and retrying on them would place
                # this user's order on the operator's brokerage account and then
                # serve the operator's holdings back as if they were the user's.
                # Their session simply has to be renewed by them.
                from trinetra.broker import registry

                registry.forget(self.ctx, "groww")
                raise BrokerError(
                    "Your Groww session is no longer valid — Groww access tokens "
                    "expire daily. Use link_broker to reconnect your Groww account, "
                    "then try again. No order was placed."
                ) from exc

            # Single-user CLI: the process config *is* this user's config, so a
            # transparent re-auth is correct.
            log.warning("Groww session expired — re-authenticating once…")
            groww_client.reset_client()
            self._client = groww_client.get_client(force_refresh=True)
            try:
                return invoke(self._client)
            except Exception as exc2:  # noqa: BLE001
                raise BrokerError(f"Groww {fn_name} failed after re-auth: {exc2}") from exc2

    def _const(self, prefix: str, value: str) -> str:
        """Resolve a Groww SDK constant (e.g. EXCHANGE_NSE) defensively, falling
        back to the literal value if the SDK names it differently."""
        return getattr(self._client, f"{prefix}_{value}", value)

    # ------------------------------------------------------------------ #
    # orders
    # ------------------------------------------------------------------ #
    def place_order(self, req: OrderRequest, reference_price: float | None = None) -> OrderResult:
        req = req.normalised()
        # Market / SL-market orders are capped against a live reference price. If
        # the caller didn't supply one, fetch it here so the safety cap always has
        # data — and guard_order fails closed if no price can be obtained.
        if reference_price is None and req.order_type in ("MARKET", "SL_M"):
            from trinetra import market_data  # lazy: avoids import cycle

            # Qualify with the exchange — an unqualified symbol resolves to the
            # default (NSE), so a BSE order could be capped against the wrong
            # instrument's price.
            reference_price = market_data.try_ltp(f"{req.exchange}_{req.trading_symbol}")
        self.guard_order(req, reference_price)

        order_type_const = {
            "MARKET": "MARKET", "LIMIT": "LIMIT",
            "SL": "STOP_LOSS", "SL_M": "STOP_LOSS_MARKET",
        }[req.order_type]
        params = dict(
            trading_symbol=req.trading_symbol,
            quantity=req.quantity,
            validity=self._const("VALIDITY", req.validity),
            exchange=self._const("EXCHANGE", req.exchange),
            segment=self._const("SEGMENT", req.segment),
            product=self._const("PRODUCT", req.product),
            order_type=self._const("ORDER_TYPE", order_type_const),
            transaction_type=self._const("TRANSACTION_TYPE", req.transaction_type),
            price=req.price if req.order_type in ("LIMIT", "SL") else 0.0,
            order_reference_id=req.reference_id,
        )
        if req.trigger_price:
            params["trigger_price"] = req.trigger_price

        log.info(
            "LIVE order → %s %s x%d (%s/%s) ref=%s",
            req.transaction_type, req.trading_symbol, req.quantity,
            req.order_type, req.product, req.reference_id,
        )
        resp = self._call("place_order", **params)

        order_id = resp.get("groww_order_id") or resp.get("order_id")
        status = (resp.get("order_status") or resp.get("status") or "placed").lower()
        return OrderResult(
            status=status,
            transaction_type=req.transaction_type,
            trading_symbol=req.trading_symbol,
            quantity=req.quantity,
            order_type=req.order_type,
            product=req.product,
            mode=self.mode,
            order_id=order_id,
            price=req.price or None,
            trigger_price=req.trigger_price,
            average_price=resp.get("average_price") or resp.get("filled_price"),
            estimated_value=req.estimated_value(reference_price) or None,
            reference_id=req.reference_id,
            exchange=req.exchange,
            message=resp.get("remark") or resp.get("message"),
            raw=resp,
        )

    def cancel_order(self, order_id: str, segment: str = "CASH") -> dict[str, Any]:
        log.info("LIVE cancel → %s", order_id)
        resp = self._call(
            "cancel_order",
            groww_order_id=order_id,
            segment=self._const("SEGMENT", segment),
        )
        return {
            "order_id": order_id,
            "status": resp.get("order_status") or resp.get("status") or "cancel_requested",
            "raw": resp,
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
        # Groww needs the (possibly unchanged) order_type + quantity on modify.
        status = self._call(
            "get_order_status", groww_order_id=order_id,
            segment=self._const("SEGMENT", segment),
        )
        ot = (order_type or status.get("order_type") or "LIMIT").upper()
        ot = {"SL": "STOP_LOSS", "SL_M": "STOP_LOSS_MARKET"}.get(ot, ot)
        qty = quantity if quantity is not None else status.get("quantity")
        if qty is None:
            raise BrokerError(
                f"Could not determine the quantity for order {order_id} (broker "
                "did not report it). Pass quantity explicitly to modify this order."
            )
        try:
            qty = int(qty)
        except (TypeError, ValueError) as exc:
            raise BrokerError(f"Invalid quantity {qty!r} for modify_order: {exc}") from exc
        kwargs: dict[str, Any] = dict(
            groww_order_id=order_id,
            segment=self._const("SEGMENT", segment),
            order_type=self._const("ORDER_TYPE", ot),
            quantity=qty,
        )
        if price is not None:
            kwargs["price"] = price
        if trigger_price is not None:
            kwargs["trigger_price"] = trigger_price
        log.info("LIVE modify -> %s qty=%s price=%s", order_id, qty, price)
        resp = self._call("modify_order", **kwargs)
        return {"order_id": order_id, "status": resp.get("order_status", "modified"), "raw": resp}

    def get_order_status(self, order_id: str, segment: str = "CASH") -> dict[str, Any]:
        resp = self._call(
            "get_order_status",
            groww_order_id=order_id,
            segment=self._const("SEGMENT", segment),
        )
        return resp

    def get_order_history(self, limit: int = 20, segment: str = "CASH") -> list[dict[str, Any]]:
        resp = self._call(
            "get_order_list",
            segment=self._const("SEGMENT", segment),
            page=0,
            page_size=min(max(limit, 1), 100),
        )
        orders = resp.get("order_list", resp.get("orders", resp if isinstance(resp, list) else []))
        return orders[:limit]

    # ------------------------------------------------------------------ #
    # portfolio
    # ------------------------------------------------------------------ #
    def _ltp_map(self, instruments: list, segment: str = "CASH") -> dict[str, float]:
        """Batch LTP lookup (Groww caps at 50 symbols per call)."""
        tokens = [inst.exchange_token for inst in instruments]
        out: dict[str, float] = {}
        for i in range(0, len(tokens), 50):
            chunk = tuple(tokens[i : i + 50])
            if not chunk:
                continue
            try:
                resp = self._call(
                    "get_ltp",
                    exchange_trading_symbols=chunk,
                    segment=self._const("SEGMENT", segment),
                )
                # response maps "NSE_RELIANCE" -> ltp
                for k, v in (resp or {}).items():
                    try:
                        out[k] = float(v)
                    except (TypeError, ValueError):
                        continue
            except BrokerError as exc:
                log.debug("LTP batch failed (non-fatal): %s", exc)
        return out

    def get_holdings(self) -> list[Holding]:
        resp = self._call("get_holdings_for_user")
        rows = resp.get("holdings", resp if isinstance(resp, list) else [])

        instruments = []
        for h in rows:
            sym = h.get("trading_symbol")
            if sym:
                instruments.append(normalize(sym))
        ltp = self._ltp_map(instruments)

        holdings: list[Holding] = []
        for h in rows:
            sym = h.get("trading_symbol")
            if not sym:
                continue
            qty = int(h.get("quantity", 0) or 0)
            avg = float(h.get("average_price", 0) or 0)
            inst = normalize(sym)
            last = ltp.get(inst.exchange_token)
            invested = round(qty * avg, 2) if avg else None
            cur = round(qty * last, 2) if last else None
            pnl = round(cur - invested, 2) if (cur is not None and invested is not None) else None
            pnl_pct = round((pnl / invested) * 100, 2) if (pnl is not None and invested) else None
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
                )
            )
        return holdings

    def get_positions(self, segment: str | None = None) -> list[Position]:
        kwargs = {}
        if segment:
            kwargs["segment"] = self._const("SEGMENT", segment)
        resp = self._call("get_positions_for_user", **kwargs)
        rows = resp.get("positions", resp if isinstance(resp, list) else [])

        positions: list[Position] = []
        for p in rows:
            sym = p.get("trading_symbol")
            if not sym:
                continue
            positions.append(
                Position(
                    trading_symbol=sym,
                    quantity=int(p.get("quantity", 0) or 0),
                    product=p.get("product", ""),
                    segment=p.get("segment", segment or "CASH"),
                    average_price=p.get("net_price") or p.get("credit_price"),
                    realised_pnl=p.get("realised_pnl"),
                )
            )
        return positions

    def get_funds(self) -> Funds:
        resp = self._call("get_available_margin_details")
        eq = resp.get("equity_margin_details", {}) or {}

        def _first(*vals: Any) -> float:
            """First value that is actually present (a legitimate 0.0 counts)."""
            for v in vals:
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
            return 0.0

        available = _first(
            resp.get("clear_cash"),
            eq.get("cnc_balance_available"),
            eq.get("mis_balance_available"),
        )
        used = _first(resp.get("net_margin_used"), eq.get("net_equity_margin_used"))
        net = _first(resp.get("clear_cash"), available)
        return Funds(
            available_cash=available,
            margin_used=used,
            net=net,
            mode=self.mode,
            detail={
                "cnc_balance_available": eq.get("cnc_balance_available"),
                "mis_balance_available": eq.get("mis_balance_available"),
                "collateral_available": resp.get("collateral_available"),
            },
        )
