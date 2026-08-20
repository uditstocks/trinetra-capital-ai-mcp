"""Per-user session context — who a call acts for, and under what limits.

A pure value object. `settings` (trinetra/config.py) holds process-level defaults
read from .env; anything a *user* owns — trading mode, their order cap, where
their data lives — belongs here, so one process can serve many accounts.

    CLI / local MCP -> default_context(), context_for_user()
    hosted MCP      -> context_for_principal(), built from the authenticated user

Persistence lives in trinetra/store.py, deliberately not here: the context says
who you are, the store says where the bytes go.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

from trinetra.config import TradingMode, settings

DEFAULT_USER_ID = "local"
_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def data_root() -> Path:
    """Root of all per-user data on the local backend. Override with TRINETRA_DATA_DIR."""
    override = os.getenv("TRINETRA_DATA_DIR")
    return Path(override).expanduser() if override else Path.home() / ".trinetra"


def safe_user_id(user_id: str) -> str:
    """Sanitise a user id so it can never escape the data root."""
    cleaned = _UNSAFE.sub("_", (user_id or "").strip())[:64]
    return cleaned or DEFAULT_USER_ID


@dataclass(frozen=True)
class SessionContext:
    """Everything the broker and service layers need to act for one account."""

    user_id: str
    trading_mode: TradingMode
    portfolio_file: Path
    data_dir: Path
    max_order_value: float
    default_product: str
    default_exchange: str
    paper_starting_cash: float
    # Set on the hosted backend, where rows are keyed by database id rather than
    # by a directory name. None on the local file backend.
    account_id: str | None = None
    broker_name: str = "paper"

    @property
    def is_live(self) -> bool:
        return self.trading_mode is TradingMode.LIVE

    @property
    def account_file(self) -> Path:
        return self.data_dir / "account.json"

    @property
    def audit_file(self) -> Path:
        return self.data_dir / "orders_audit.log"

    def with_mode(self, mode: TradingMode) -> SessionContext:
        return replace(self, trading_mode=mode)


def default_context() -> SessionContext:
    """Process-default context, derived from `settings` on every call.

    Deliberately rebuilt each time rather than cached, so the CLI — and the test
    suite, which overrides settings in fixtures — always sees current values.
    """
    return SessionContext(
        user_id=DEFAULT_USER_ID,
        trading_mode=settings.trading_mode,
        portfolio_file=settings.portfolio_file,  # legacy root path, unchanged
        # Account record and audit log belong beside the portfolio they describe,
        # which also keeps them out of the repo when a test redirects it.
        data_dir=settings.portfolio_file.parent,
        max_order_value=settings.max_order_value,
        default_product=settings.default_product,
        default_exchange=settings.default_exchange,
        paper_starting_cash=settings.paper_starting_cash,
    )


def context_for_user(
    user_id: str = DEFAULT_USER_ID,
    trading_mode: TradingMode | None = None,
) -> SessionContext:
    """Context for a local MCP user, isolated in its own directory."""
    uid = safe_user_id(user_id)
    udir = data_root() / "users" / uid
    return SessionContext(
        user_id=uid,
        trading_mode=trading_mode or TradingMode.PAPER,
        portfolio_file=udir / "portfolio.json",
        data_dir=udir,
        max_order_value=settings.max_order_value,
        default_product=settings.default_product,
        default_exchange=settings.default_exchange,
        paper_starting_cash=settings.paper_starting_cash,
    )


def context_for_account(
    user_id: str,
    account_id: str,
    trading_mode: TradingMode,
    max_order_value: float,
    broker_name: str = "paper",
    default_product: str | None = None,
    default_exchange: str | None = None,
    paper_starting_cash: float | None = None,
) -> SessionContext:
    """Context for a hosted user, built from their database row.

    The filesystem paths are still populated so the shape stays uniform, but the
    database store never reads them.
    """
    udir = data_root() / "users" / safe_user_id(user_id)
    return SessionContext(
        user_id=user_id,
        trading_mode=trading_mode,
        portfolio_file=udir / "portfolio.json",
        data_dir=udir,
        max_order_value=max_order_value,
        default_product=default_product or settings.default_product,
        default_exchange=default_exchange or settings.default_exchange,
        paper_starting_cash=(
            paper_starting_cash if paper_starting_cash is not None
            else settings.paper_starting_cash
        ),
        account_id=account_id,
        broker_name=broker_name,
    )
