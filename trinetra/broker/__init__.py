"""Broker layer.

`get_broker()` returns the singleton broker implementation chosen by the
configured trading mode:

    paper -> PaperBroker  (simulated fills, portfolio.json)
    live  -> GrowwBroker  (real orders via the Groww API)

The rest of the app depends only on the `Broker` interface, so swapping or
adding brokers never touches the agents or tools.
"""

from __future__ import annotations

from trinetra.session import SessionContext, default_context
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

# Legacy single-user slot (the CLI path). Kept as a module attribute because the
# test suite resets it directly.
_broker: Broker | None = None
# Per-user cache for the MCP path, keyed by "user_id|mode".
_brokers: dict[str, Broker] = {}


def _build(ctx: SessionContext) -> Broker:
    if ctx.is_live:
        from trinetra.broker.groww_broker import GrowwBroker

        log.warning("LIVE trading mode active — orders will hit a real account.")
        return GrowwBroker(ctx)

    from trinetra.broker.paper_broker import PaperBroker

    log.info("PAPER trading mode - orders are simulated (no real money).")
    return PaperBroker(ctx)


def get_broker(ctx: SessionContext | None = None, force: bool = False) -> Broker:
    """Broker bound to `ctx`, or to the process default when ctx is None.

    The default (CLI) broker keeps its own slot so its lazily-resolved context
    still tracks runtime settings changes. Explicit contexts are cached per user
    and mode, so two MCP users never share broker state.
    """
    global _broker
    if ctx is None:
        if _broker is None or force:
            _broker = _build(default_context())
        return _broker

    if ctx.is_live and ctx.account_id:
        # Hosted live: the adapter is built from the user's vaulted credentials
        # and cached by the registry on its own short TTL, so a revoked link or a
        # kill switch stops working promptly rather than living in a cache here.
        from trinetra.broker import registry

        return registry.live_broker(ctx)

    key = f"{ctx.user_id}|{ctx.trading_mode.value}"
    if force or key not in _brokers:
        _brokers[key] = _build(ctx)
    return _brokers[key]


def reset_brokers() -> None:
    """Drop every cached broker (used by tests and on mode switches)."""
    global _broker
    _broker = None
    _brokers.clear()
    from trinetra.broker import registry

    registry.clear()


__all__ = [
    "Broker",
    "BrokerError",
    "Funds",
    "Holding",
    "OrderRequest",
    "OrderResult",
    "Position",
    "get_broker",
    "reset_brokers",
]
