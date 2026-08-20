"""Plumbing the MCP tools rely on: whose account, valid input, order tokens.

Three concerns, all about the boundary between an AI host and the trading core:

- Which account a call acts on. Phase 1 runs one stdio server per user, so the
  id comes from the environment. The *trading mode* is read from the persisted
  account record, never from a tool argument, so nothing the host reads can flip
  a session into live trading.
- Argument validation. A host can pass anything, including values it inferred
  rather than values the user said.
- Confirmation tokens. An order preview hands back an opaque token; the
  instruction it maps to is held here, so what was previewed is what gets placed.
"""

from __future__ import annotations

import contextlib
import io
import os
import secrets
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token

from trinetra import store
from trinetra.config import TradingMode
from trinetra.session import (
    DEFAULT_USER_ID,
    SessionContext,
    context_for_account,
    context_for_user,
)


class NotAuthenticated(RuntimeError):
    """Raised when a hosted request carries no verified identity."""


# --------------------------------------------------------------------------- #
# session
# --------------------------------------------------------------------------- #
def current_user_id() -> str:
    return os.getenv("TRINETRA_USER_ID", DEFAULT_USER_ID)


def current_context() -> SessionContext:
    """The account this call acts for.

    Hosted: the verified token's subject, resolved to a database account.
    Local:  the process's user id, backed by files.

    Fails closed. If a database is configured we are running hosted, so an
    unauthenticated call is refused rather than falling back to the shared local
    account — that fallback would hand an anonymous caller someone's portfolio.
    """
    token = get_access_token()
    if token is not None:
        return _hosted_context(token)
    if store.database_url() is not None:
        raise NotAuthenticated(
            "This server requires authentication and the request carried no valid token."
        )
    return _local_context()


def _local_context() -> SessionContext:
    base = context_for_user(current_user_id())
    account = store.load_account(base)
    if (account or {}).get("mode") == TradingMode.LIVE.value:
        return base.with_mode(TradingMode.LIVE)
    return base


def _hosted_context(token) -> SessionContext:
    from trinetra import db

    # Only the verified subject identifies a person. client_id identifies the
    # OAuth *client* — every user of the same AI host shares one, so falling back
    # to it would put them all in a single account.
    subject = getattr(token, "subject", None)
    if not subject:
        raise NotAuthenticated(
            "The access token carries no subject claim, so it cannot be tied to an "
            "account."
        )
    user_id, account_id = db.upsert_user(subject, getattr(token, "email", None))
    row = db.load_account_row(account_id)
    if row is None:
        raise NotAuthenticated(f"Account {account_id} disappeared during the request.")

    return context_for_account(
        user_id=user_id,
        account_id=account_id,
        trading_mode=TradingMode(row.mode),
        max_order_value=row.max_order_value,
        broker_name=row.broker,
        default_product=row.default_product,
        default_exchange=row.default_exchange,
        paper_starting_cash=row.starting_cash,
    )


def require_account(ctx: SessionContext) -> dict[str, Any] | None:
    """A structured 'no account yet' payload, or None when the user is set up."""
    if store.account_exists(ctx):
        return None
    return {
        "status": "not_set_up",
        "message": "This user has no Trinetra account yet.",
        "next_step": "Call setup_account(mode='paper') to create a free paper-trading "
                     "account with virtual cash. No broker credentials are needed.",
    }


# --------------------------------------------------------------------------- #
# input guards
# --------------------------------------------------------------------------- #
# A single order can never be for more shares than this, whatever the model says.
# The rupee cap is the real control; this just stops absurd integers.
MAX_QUANTITY = 1_000_000
MAX_SYMBOL_LEN = 40


class ToolInputError(ValueError):
    """A bad tool argument, rendered back as a structured rejection."""


def positive_int(value: Any, name: str, maximum: int = MAX_QUANTITY) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ToolInputError(f"{name} must be a whole number, got {value!r}.") from None
    if parsed <= 0:
        raise ToolInputError(f"{name} must be greater than 0, got {parsed}.")
    if parsed > maximum:
        raise ToolInputError(f"{name} must be at most {maximum:,}, got {parsed:,}.")
    return parsed


def non_negative_float(value: Any, name: str) -> float:
    if value in (None, ""):
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ToolInputError(f"{name} must be a number, got {value!r}.") from None
    if parsed < 0:
        raise ToolInputError(f"{name} cannot be negative, got {parsed}.")
    if parsed != parsed or parsed in (float("inf"), float("-inf")):  # NaN / inf
        raise ToolInputError(f"{name} must be a finite number.")
    return parsed


def bounded_int(value: Any, name: str, low: int, high: int, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ToolInputError(f"{name} must be a whole number, got {value!r}.") from None
    return max(low, min(high, parsed))


def clean_symbol(value: Any, name: str = "symbol") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{name} is required.")
    cleaned = value.strip()
    if len(cleaned) > MAX_SYMBOL_LEN:
        raise ToolInputError(f"{name} is too long (max {MAX_SYMBOL_LEN} characters).")
    return cleaned


def choice(value: Any, name: str, allowed: Iterable[str], default: str = "") -> str:
    options = [a.lower() for a in allowed]
    raw = (value or default or "").strip().lower()
    if not raw:
        return default
    if raw not in options:
        raise ToolInputError(f"{name} must be one of {', '.join(options)}, got {value!r}.")
    return raw


@contextlib.contextmanager
def quiet_stdout():
    """Keep library chatter off stdout while a tool runs.

    The stdio transport uses stdout for the MCP protocol itself, so a stray
    `print` from a data library would corrupt the stream. FastMCP holds the real
    stdout outside the tool body, so swapping it here is safe.
    """
    original = sys.stdout
    sys.stdout = io.StringIO()
    try:
        yield
    finally:
        noise = sys.stdout.getvalue()
        sys.stdout = original
        if noise.strip():
            # A console-encoding problem on the diagnostic path must not break a
            # tool call that otherwise succeeded.
            with contextlib.suppress(UnicodeEncodeError, ValueError, OSError):
                print(noise, file=sys.stderr)


# --------------------------------------------------------------------------- #
# order confirmation tokens
# --------------------------------------------------------------------------- #
TTL_SECONDS = 180.0
_MAX_PENDING = 32  # bounds memory if a host previews repeatedly without confirming


@dataclass
class _Pending:
    user_id: str
    order: dict[str, Any]
    preview: dict[str, Any]
    expires_at: float


_lock = threading.Lock()
_pending: dict[str, _Pending] = {}


def _purge_locked(now: float) -> None:
    for token in [t for t, p in _pending.items() if p.expires_at <= now]:
        del _pending[token]


def issue_token(ctx: SessionContext, order: dict[str, Any],
                preview: dict[str, Any]) -> tuple[str, int]:
    """Store a validated order and return (token, ttl_seconds).

    Hosted runs keep pending confirmations in the database, so they survive a
    restart and are visible to every replica. Local runs keep them in memory.
    """
    token = f"tcai_{secrets.token_urlsafe(18)}"
    if store.database_url() is not None:
        from trinetra import db

        db.store_confirmation(ctx, token, order, preview, TTL_SECONDS)
        return token, int(TTL_SECONDS)

    now = time.monotonic()
    with _lock:
        _purge_locked(now)
        if len(_pending) >= _MAX_PENDING:
            del _pending[min(_pending, key=lambda t: _pending[t].expires_at)]
        _pending[token] = _Pending(ctx.user_id, order, preview, now + TTL_SECONDS)
    return token, int(TTL_SECONDS)


def redeem_token(ctx: SessionContext, token: str) -> tuple[dict[str, Any] | None,
                                                           dict[str, Any] | None]:
    """Consume a token, returning (pending, None) or (None, error).

    Removed on any matching attempt, so a replay can never place a second order.
    """
    token = (token or "").strip()
    if store.database_url() is not None:
        from trinetra import db

        pending = db.take_confirmation(ctx, token)
        return (pending, None) if pending else (None, _token_error())

    now = time.monotonic()
    with _lock:
        _purge_locked(now)
        pending = _pending.pop(token, None)

    if pending is None:
        return None, _token_error()
    if pending.user_id != ctx.user_id:
        return None, {"status": "rejected",
                      "error": "Confirmation token does not belong to this account."}
    return {"order": pending.order, "preview": pending.preview}, None


def _token_error() -> dict[str, Any]:
    return {
        "status": "rejected",
        "error": "That confirmation token is unknown, already used, or expired "
                 f"(tokens last {int(TTL_SECONDS)} seconds). Call place_order "
                 "again for a fresh preview, and show it to the user before "
                 "confirming.",
    }


def clear_tokens() -> None:
    """Drop all pending confirmations (tests, and on shutdown)."""
    with _lock:
        _pending.clear()
