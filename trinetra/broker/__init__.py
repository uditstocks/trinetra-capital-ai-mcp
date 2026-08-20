"""Broker layer.

`get_broker()` returns the singleton broker implementation chosen by the
configured trading mode:

    paper -> PaperBroker  (simulated fills, portfolio.json)
    live  -> GrowwBroker  (real orders via the Groww API)

The rest of the app depends only on the `Broker` interface, so swapping or
adding brokers never touches the agents or tools.
"""

from __future__ import annotations

from trinetra.config import settings
from trinetra.logging_setup import get_logger
from trinetra.broker.base import (
    Broker,
    BrokerError,
    Funds,
    Holding,
    OrderRequest,
    OrderResult,
    Position,
)

log = get_logger(__name__)

_broker: Broker | None = None


def get_broker(force: bool = False) -> Broker:
    global _broker
    if _broker is not None and not force:
        return _broker

    if settings.is_live:
        from trinetra.broker.groww_broker import GrowwBroker

        log.warning("LIVE trading mode active — orders will hit the real Groww account.")
        _broker = GrowwBroker()
    else:
        from trinetra.broker.paper_broker import PaperBroker

        log.info("PAPER trading mode - orders are simulated (no real money).")
        _broker = PaperBroker()
    return _broker


__all__ = [
    "get_broker",
    "Broker",
    "BrokerError",
    "OrderRequest",
    "OrderResult",
    "Holding",
    "Position",
    "Funds",
]
