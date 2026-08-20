"""Live broker backed by Zerodha Kite Connect (equity cash segment).

Maps Kite's vocabulary onto the normalised `Broker` interface, so nothing above
this layer knows which broker is active — the same safety cap, the same
preview/confirm flow, the same dataclasses.

Two things differ from Groww and shape the code:

- **Login is a browser redirect, not a key exchange.** Kite issues a daily access
  token from a `request_token` the user's browser hands back after logging in at
  Kite. That happens in the linking flow (trinetra/brokerlink.py + the web
  callback); this class receives the resulting access token.
- **The access token expires every morning.** There is no way to refresh it
  without the user visiting Kite again, so an expired token raises a clear
  "re-link" error instead of a generic failure.
"""

from __future__ import annotations

from typing import Any, ClassVar

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

log = get_logger(__name__)

RELINK_MESSAGE = (
    "Your Zerodha session has expired — Kite access tokens last one trading day. "
    "Use link_broker to reconnect your Zerodha account."
)


def _kite():
    try:
        from kiteconnect import KiteConnect
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise BrokerError(
            "The 'kiteconnect' package is not installed. Run: pip install kiteconnect"
        ) from exc
    return KiteConnect


def build_client(api_key: str, access_token: str):
    """A ready-to-use Kite client. Credentials are passed in, never read from
    global config — this broker always acts for a specific user."""
    client = _kite()(api_key=api_key)
    client.set_access_token(access_token)
    return client


def login_url(api_key: str) -> str:
    """Where to send the user to authorise Kite."""
    return _kite()(api_key=api_key).login_url()


def exchange_request_token(api_key: str, api_secret: str, request_token: str) -> dict[str, Any]:
    """Trade the browser's request_token for a daily access token."""
    try:
        session = _kite()(api_key=api_key).generate_session(
            request_token, api_secret=api_secret
        )
    except Exception as exc:  # noqa: BLE001 - normalise any SDK/HTTP error
        raise BrokerError(f"Zerodha login could not be completed: {exc}") from exc
    if not session.get("access_token"):
        raise BrokerError("Zerodha returned no access token.")
    return session


def _is_token_error(exc: Exception) -> bool:
    return type(exc).__name__ == "TokenException"


def _num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None  # drop NaN


class ZerodhaBroker(Broker):
    name = "zerodha"
    mode = "live"

    # Our normalised vocabulary -> Kite's. Class-level and immutable so an
    # instance can never mutate the mapping another user's order relies on.
    _ORDER_TYPES: ClassVar[dict[str, str]] = {
        "MARKET": "MARKET", "LIMIT": "LIMIT", "SL": "SL", "SL_M": "SL-M",
    }
    _PRODUCTS: ClassVar[dict[str, str]] = {"CNC": "CNC", "MIS": "MIS"}

    def __init__(self, ctx=None, credentials=None, access_token: str | None = None) -> None:
        super().__init__(ctx)
        if credentials is None:
            raise BrokerError("ZerodhaBroker requires the user's linked credentials.")
        self._api_key = credentials.require("api_key")
        token = access_token or credentials.get("access_token")
        if not token:
            raise BrokerError(RELINK_MESSAGE)
        self._client = build_client(self._api_key, token)

    def _call(self, method: str, **kwargs) -> Any:
        try:
            return getattr(self._client, method)(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if _is_token_error(exc):
                raise BrokerError(RELINK_MESSAGE) from exc
            raise BrokerError(f"Zerodha {method} failed: {exc}") from exc

    # ------------------------------------------------------------------ #
    # orders
    # ------------------------------------------------------------------ #
    def place_order(self, req: OrderRequest, reference_price: float | None = None) -> OrderResult:
        req = req.normalised()

        # A market order carries no price of its own, so the cap needs a live
        # reference. Fetch one rather than letting the guard fail closed.
        if reference_price is None and req.order_type in ("MARKET", "SL_M"):
            reference_price = self._last_price(req)
        self.validate_order(req, reference_price)

        order_type = self._ORDER_TYPES.get(req.order_type)
        product = self._PRODUCTS.get(req.product)
        if order_type is None or product is None:
            raise BrokerError(
                f"Zerodha does not support order_type={req.order_type} "
                f"product={req.product} in the cash segment."
            )

        params: dict[str, Any] = {
            "variety": "regular",
            "exchange": req.exchange,
            "tradingsymbol": req.trading_symbol,
            "transaction_type": req.transaction_type,
            "quantity": req.quantity,
            "product": product,
            "order_type": order_type,
            "validity": "DAY",
            # Kite echoes the tag back on the order, which makes reconciliation
            # against our own records straightforward.
            "tag": req.reference_id[:20],
        }
        if req.order_type in ("LIMIT", "SL"):
            params["price"] = req.price
        if req.trigger_price:
            params["trigger_price"] = req.trigger_price

        order_id = self._call("place_order", **params)
        return OrderResult(
            status="placed",
            transaction_type=req.transaction_type,
            trading_symbol=req.trading_symbol,
            quantity=req.quantity,
            order_type=req.order_type,
            product=req.product,
            mode=self.mode,
            order_id=str(order_id),
            price=req.price or None,
            trigger_price=req.trigger_price,
            estimated_value=req.estimated_value(reference_price),
            reference_id=req.reference_id,
            exchange=req.exchange,
            message="Sent to Zerodha. Use get_order_status to see whether it filled.",
        )

    def cancel_order(self, order_id: str, segment: str = "CASH") -> dict[str, Any]:
        self._call("cancel_order", variety="regular", order_id=order_id)
        return {"order_id": order_id, "status": "cancelled"}

    def modify_order(
        self,
        order_id: str,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
        order_type: str | None = None,
        segment: str = "CASH",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"variety": "regular", "order_id": order_id}
        if quantity:
            params["quantity"] = quantity
        if price:
            params["price"] = price
        if trigger_price:
            params["trigger_price"] = trigger_price
        if order_type:
            params["order_type"] = self._ORDER_TYPES.get(order_type.upper(), order_type)
        self._call("modify_order", **params)
        return {"order_id": order_id, "status": "modified"}

    def get_order_status(self, order_id: str, segment: str = "CASH") -> dict[str, Any]:
        history = self._call("order_history", order_id=order_id) or []
        if not history:
            return {"order_id": order_id, "status": "unknown"}
        latest = history[-1]
        return {
            "order_id": order_id,
            "status": str(latest.get("status", "")).lower(),
            "trading_symbol": latest.get("tradingsymbol"),
            "quantity": latest.get("quantity"),
            "filled_quantity": latest.get("filled_quantity"),
            "average_price": _num(latest.get("average_price")),
            "message": latest.get("status_message"),
        }

    def get_order_history(self, limit: int = 20, segment: str = "CASH") -> list[dict[str, Any]]:
        orders = self._call("orders") or []
        rows = [
            {
                "order_id": o.get("order_id"),
                "symbol": o.get("tradingsymbol"),
                "exchange": o.get("exchange"),
                "action": str(o.get("transaction_type", "")).lower(),
                "shares": o.get("quantity"),
                "price": _num(o.get("average_price")) or _num(o.get("price")),
                "order_type": o.get("order_type"),
                "product": o.get("product"),
                "status": str(o.get("status", "")).lower(),
                "timestamp": str(o.get("order_timestamp") or ""),
                "mode": "live",
            }
            for o in orders
        ]
        return list(reversed(rows))[:limit]

    # ------------------------------------------------------------------ #
    # portfolio
    # ------------------------------------------------------------------ #
    def get_holdings(self) -> list[Holding]:
        holdings: list[Holding] = []
        for row in self._call("holdings") or []:
            quantity = int(row.get("quantity") or 0) + int(row.get("t1_quantity") or 0)
            if quantity <= 0:
                continue
            average = _num(row.get("average_price")) or 0.0
            last = _num(row.get("last_price"))
            invested = round(quantity * average, 2)
            current = round(quantity * last, 2) if last else None
            pnl = round(current - invested, 2) if current is not None else None
            holdings.append(Holding(
                trading_symbol=row.get("tradingsymbol", ""),
                quantity=quantity,
                average_price=round(average, 2),
                last_price=last,
                invested=invested,
                current_value=current,
                pnl=pnl,
                pnl_pct=round(pnl / invested * 100, 2) if pnl is not None and invested else None,
                # Kite exposes no booked-P&L figure for delivery holdings, so we
                # leave it unset rather than inventing one.
            ))
        return holdings

    def get_positions(self, segment: str | None = None) -> list[Position]:
        book = self._call("positions") or {}
        return [
            Position(
                trading_symbol=row.get("tradingsymbol", ""),
                quantity=int(row.get("quantity") or 0),
                product=row.get("product", ""),
                segment="CASH",
                average_price=_num(row.get("average_price")),
                last_price=_num(row.get("last_price")),
                realised_pnl=_num(row.get("realised")),
                unrealised_pnl=_num(row.get("unrealised")),
            )
            for row in (book.get("net") or [])
            if int(row.get("quantity") or 0) != 0
        ]

    def get_funds(self) -> Funds:
        margins = self._call("margins", segment="equity") or {}
        available = margins.get("available", {}) or {}
        utilised = margins.get("utilised", {}) or {}
        cash = _num(available.get("live_balance"))
        if cash is None:
            cash = _num(available.get("cash")) or 0.0
        used = _num(utilised.get("debits")) or 0.0
        return Funds(
            available_cash=cash,
            margin_used=used,
            net=_num(margins.get("net")),
            mode=self.mode,
        )

    # ------------------------------------------------------------------ #
    def _last_price(self, req: OrderRequest) -> float | None:
        key = f"{req.exchange}:{req.trading_symbol}"
        try:
            quote = self._call("ltp", instruments=[key]) or {}
            return _num((quote.get(key) or {}).get("last_price"))
        except BrokerError as exc:
            log.debug("Zerodha LTP lookup failed for %s: %s", key, exc)
            return None
