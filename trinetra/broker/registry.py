"""Which broker acts for a user, and how one gets built.

Adding a broker means registering an adapter here plus an entry in
`trinetra.brokerlink.BROKERS` — never editing the order path.

Credentials are opened from the vault at construction and held only for the life
of the adapter, which is cached briefly per user so a burst of tool calls does
not re-authenticate each time. The cache is keyed by user *and* broker and has a
short TTL, so a revoked link stops working promptly.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from trinetra.broker.base import Broker, BrokerError
from trinetra.logging_setup import get_logger
from trinetra.session import SessionContext

log = get_logger(__name__)

# Short enough that unlinking takes effect quickly; long enough that a
# multi-tool turn does not re-authenticate on every call.
CACHE_TTL_SECONDS = 300.0

_lock = threading.Lock()
_cache: dict[str, tuple[Broker, float]] = {}


def _build_groww(ctx: SessionContext) -> Broker:
    from trinetra import brokerlink
    from trinetra.broker.groww_broker import GrowwBroker
    from trinetra.broker import groww_client

    credentials = brokerlink.load_credentials(ctx, "groww")
    return GrowwBroker(ctx, client=groww_client.build_client(credentials))


def _build_zerodha(ctx: SessionContext) -> Broker:
    from trinetra import brokerlink
    from trinetra.broker.zerodha_broker import ZerodhaBroker

    credentials = brokerlink.load_credentials(ctx, "zerodha")
    return ZerodhaBroker(ctx, credentials=credentials)


BUILDERS: dict[str, Callable[[SessionContext], Broker]] = {
    "groww": _build_groww,
    "zerodha": _build_zerodha,
}


def live_broker(ctx: SessionContext) -> Broker:
    """The user's linked live broker, built from their sealed credentials."""
    from trinetra import brokerlink

    link = brokerlink.active_link(ctx)
    if link is None:
        raise BrokerError(
            "No broker account is linked, so live orders cannot be placed. "
            "Use link_broker to connect Groww or Zerodha."
        )
    builder = BUILDERS.get(link["broker"])
    if builder is None:
        raise BrokerError(f"No adapter is registered for {link['broker']!r}.")

    key = f"{ctx.user_id}|{link['broker']}"
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[1] > now:
            return hit[0]

    broker = builder(ctx)
    with _lock:
        _cache[key] = (broker, now + CACHE_TTL_SECONDS)
    return broker


def forget_user(user_id: str, broker: str | None = None) -> None:
    """Drop cached adapters for a user id.

    Must be called whenever the stored credentials change, not only on unlink: a
    cached adapter holds an authenticated session built from the *old* secrets,
    so a re-link would otherwise keep using them until the TTL lapsed. That is
    routine for Zerodha, whose session has to be renewed every trading day.
    """
    with _lock:
        for key in [k for k in _cache
                    if k.startswith(f"{user_id}|")
                    and (broker is None or k.endswith(f"|{broker}"))]:
            del _cache[key]


def forget(ctx: SessionContext, broker: str | None = None) -> None:
    """Drop cached adapters for a user — on unlink, re-link and kill switch."""
    forget_user(ctx.user_id, broker)


def clear() -> None:
    with _lock:
        _cache.clear()
