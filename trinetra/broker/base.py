"""Broker interface + normalised data types.

Both PaperBroker and GrowwBroker speak this vocabulary so the agents/tools never
have to know which one is active. All money values are in INR.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

from trinetra.session import SessionContext, default_context


class BrokerError(Exception):
    """Raised for any recoverable broker problem (validation, API error)."""


# Normalised constants used across the app. The Groww broker maps these onto the
# SDK's own constants; the paper broker just stores them.
BUY = "BUY"
SELL = "SELL"
MARKET = "MARKET"
LIMIT = "LIMIT"
SL = "SL"        # stop-loss limit (needs price + trigger_price)
SL_M = "SL_M"    # stop-loss market (needs trigger_price)
ORDER_TYPES = (MARKET, LIMIT, SL, SL_M)
PRODUCT_CNC = "CNC"  # delivery
PRODUCT_MIS = "MIS"  # intraday
SEGMENT_CASH = "CASH"
VALIDITY_DAY = "DAY"


def new_reference_id() -> str:
    """Groww requires an alphanumeric reference id (hyphens allowed, <=20 chars)."""
    return f"trn-{uuid.uuid4().hex[:12]}"


@dataclass
class OrderRequest:
    trading_symbol: str
    transaction_type: str           # BUY | SELL
    quantity: int
    exchange: str = "NSE"           # NSE | BSE
    segment: str = SEGMENT_CASH
    product: str = PRODUCT_CNC      # CNC | MIS
    order_type: str = MARKET        # MARKET | LIMIT
    price: float = 0.0              # required for LIMIT, ignored for MARKET
    trigger_price: float | None = None
    validity: str = VALIDITY_DAY
    reference_id: str = field(default_factory=new_reference_id)

    def normalised(self) -> OrderRequest:
        """Return a validated copy with a bare Groww trading symbol + resolved
        exchange (so "RELIANCE.NS"/"TCS.BO" become "RELIANCE"@NSE / "TCS"@BSE).
        Raises BrokerError on bad input."""
        from trinetra.symbols import normalize  # local import: avoids any cycle

        inst = normalize(self.trading_symbol, self.exchange)

        tt = self.transaction_type.strip().upper()
        if tt not in (BUY, SELL):
            raise BrokerError(f"transaction_type must be BUY or SELL, got {self.transaction_type!r}")

        ot = self.order_type.strip().upper().replace("STOP_LOSS_MARKET", SL_M).replace(
            "STOP_LOSS", SL
        )
        if ot not in ORDER_TYPES:
            raise BrokerError(
                f"order_type must be one of MARKET/LIMIT/SL/SL_M, got {self.order_type!r}"
            )

        product = self.product.strip().upper()
        if product not in (PRODUCT_CNC, PRODUCT_MIS):
            raise BrokerError(f"product must be CNC or MIS, got {self.product!r}")

        qty = int(self.quantity)
        if qty <= 0:
            raise BrokerError(f"quantity must be a positive integer, got {self.quantity!r}")

        if ot in (LIMIT, SL) and (not self.price or self.price <= 0):
            raise BrokerError(f"{ot} orders require a positive limit price.")
        if ot in (SL, SL_M) and (not self.trigger_price or self.trigger_price <= 0):
            raise BrokerError(f"{ot} (stop-loss) orders require a positive trigger_price.")

        return OrderRequest(
            trading_symbol=inst.trading_symbol,
            transaction_type=tt,
            quantity=qty,
            exchange=inst.exchange,
            segment=self.segment.strip().upper(),
            product=product,
            order_type=ot,
            price=float(self.price or 0.0),
            trigger_price=self.trigger_price,
            validity=self.validity.strip().upper(),
            reference_id=self.reference_id,
        )

    def estimated_value(self, reference_price: float | None = None) -> float:
        """Notional exposure, priced at the WORSE of the caller's price and market.

        Taking the caller's price at face value understates a marketable limit and
        lets it slip under every rupee cap: a SELL LIMIT at ₹1 on a ₹1,400 stock
        prices as ₹1 per share but fills at ₹1,400, so 500 shares reads as ₹500
        while liquidating ₹7,00,000. The same holds for a stop-loss whose trigger
        sits far from market. Pricing at the maximum is correct in both
        directions — a BUY never pays above its limit, and a SELL never receives
        less than market for a marketable order.
        """
        ot = self.order_type.strip().upper()
        candidates = [reference_price or 0.0]
        if ot in (LIMIT, SL):
            candidates.append(self.price or 0.0)
        elif ot == SL_M:
            candidates.append(self.trigger_price or 0.0)
        return round(self.quantity * max(candidates), 2)


@dataclass
class OrderResult:
    status: str                     # placed | filled | rejected | failed
    transaction_type: str
    trading_symbol: str
    quantity: int
    order_type: str
    product: str
    mode: str                       # paper | live
    order_id: str | None = None
    price: float | None = None
    trigger_price: float | None = None
    average_price: float | None = None
    estimated_value: float | None = None
    reference_id: str | None = None
    exchange: str | None = None
    message: str | None = None
    raw: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)  # keep the agent-facing payload clean
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class Holding:
    trading_symbol: str
    quantity: int
    average_price: float
    last_price: float | None = None
    invested: float | None = None
    current_value: float | None = None
    pnl: float | None = None            # unrealized (mark-to-market on open shares)
    pnl_pct: float | None = None
    realised_pnl: float | None = None   # booked P&L on shares of this symbol already sold
    holding_days: int | None = None     # days since the first buy (delivery holding period)
    allocation_pct: float | None = None  # this holding's weight in the portfolio (%)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Position:
    trading_symbol: str
    quantity: int
    product: str
    segment: str
    average_price: float | None = None
    last_price: float | None = None
    realised_pnl: float | None = None
    unrealised_pnl: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Funds:
    available_cash: float
    margin_used: float = 0.0
    net: float | None = None
    mode: str = "paper"
    detail: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "available_cash": round(self.available_cash, 2),
            "margin_used": round(self.margin_used, 2),
            "mode": self.mode,
        }
        if self.net is not None:
            d["net"] = round(self.net, 2)
        if self.detail:
            d["detail"] = self.detail
        return d


class Broker(ABC):
    """Abstract broker. Implementations must enforce the order-value safety cap
    via `guard_order` before sending anything irreversible.

    A broker acts on behalf of exactly one `SessionContext`. Passing `ctx=None`
    binds it to the process default (the CLI's single-user case), resolved lazily
    so runtime settings changes are still picked up.
    """

    name: str = "broker"
    mode: str = "paper"

    def __init__(self, ctx: SessionContext | None = None) -> None:
        self._ctx = ctx

    @property
    def ctx(self) -> SessionContext:
        return self._ctx if self._ctx is not None else default_context()

    def guard_order(self, req: OrderRequest, reference_price: float | None = None) -> None:
        """Hard ceiling enforced for both paper and live orders.

        Fails CLOSED: if the order's notional value cannot be determined (e.g. a
        market order with no resolvable reference price), the order is REJECTED
        rather than allowed through uncapped. This closes the gap where a failed
        price lookup would otherwise let an oversized market order skip the cap.
        """
        value = req.estimated_value(reference_price)
        cap = self.ctx.max_order_value
        if not value or value <= 0:
            raise BrokerError(
                "Cannot determine this order's value — no reference price is "
                "available, so the ₹{:,.2f} safety cap (GROWW_MAX_ORDER_VALUE) "
                "cannot be enforced. Refusing the order. Fetch a live quote and "
                "retry, or place a LIMIT order with an explicit price.".format(cap)
            )
        if value > cap:
            raise BrokerError(
                f"Order value ₹{value:,.2f} exceeds the safety cap of ₹{cap:,.2f} "
                f"(GROWW_MAX_ORDER_VALUE). Reduce quantity or raise the cap."
            )

    def validate_order(self, req: OrderRequest, reference_price: float | None = None) -> None:
        """Run every pre-flight check that can be made without mutating state.

        Raises BrokerError on the first problem. An order preview calls this, and
        so does the implementation's own place_order, so a preview can never
        approve something execution would then reject. Subclasses extend it with
        their own affordability/position rules.
        """
        self.guard_order(req.normalised(), reference_price)

    @abstractmethod
    def place_order(self, req: OrderRequest, reference_price: float | None = None) -> OrderResult:
        ...

    @abstractmethod
    def cancel_order(self, order_id: str, segment: str = SEGMENT_CASH) -> dict[str, Any]:
        ...

    @abstractmethod
    def modify_order(
        self,
        order_id: str,
        quantity: int | None = None,
        price: float | None = None,
        trigger_price: float | None = None,
        order_type: str | None = None,
        segment: str = SEGMENT_CASH,
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    def get_order_status(self, order_id: str, segment: str = SEGMENT_CASH) -> dict[str, Any]:
        ...

    @abstractmethod
    def get_order_history(self, limit: int = 20, segment: str = SEGMENT_CASH) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def get_holdings(self) -> list[Holding]:
        ...

    @abstractmethod
    def get_positions(self, segment: str | None = None) -> list[Position]:
        ...

    @abstractmethod
    def get_funds(self) -> Funds:
        ...

    def performance(self) -> dict[str, Any]:
        """Realized-P&L + trade-performance analytics. Only paper mode can derive
        these from its own trade log; live brokers override or fall back to this
        honest 'not available' notice (Groww exposes no historical booked P&L for
        delivery holdings)."""
        return {
            "available": False,
            "mode": self.mode,
            "error": "Performance analytics (booked P&L, win-rate, closed "
                     "positions) are available in paper mode only. In live mode, "
                     "use your Groww order book for realized figures.",
        }

    def realized_total(self) -> float | None:
        """Cheap (no-network) total booked P&L across ALL symbols ever traded,
        including fully-closed positions. None when the broker can't derive it
        (live mode). Used by the portfolio summary's realized/overall figures."""
        return None
