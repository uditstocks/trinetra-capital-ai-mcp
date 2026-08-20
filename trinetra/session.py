"""Per-user session context and storage.

`settings` (trinetra/config.py) holds process-level defaults read from .env.
Anything a *user* owns — trading mode, where their portfolio lives, their order
cap — belongs here, so one process can serve more than one account.

    CLI  -> default_context()          keeps the legacy root portfolio.json
    MCP  -> context_for_user(user_id)  isolated under ~/.trinetra/users/<id>/

Phase 2 (hosted, multi-tenant) replaces `context_for_user` and the read/write
helpers with database-backed equivalents; their signatures stay the same.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trinetra.config import PROJECT_ROOT, TradingMode, settings
from trinetra.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_USER_ID = "local"
ACCOUNT_VERSION = 1
_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def data_root() -> Path:
    """Root of all per-user data. Override with TRINETRA_DATA_DIR."""
    override = os.getenv("TRINETRA_DATA_DIR")
    return Path(override).expanduser() if override else Path.home() / ".trinetra"


def safe_user_id(user_id: str) -> str:
    """Sanitise a user id so it can never escape the data root."""
    cleaned = _UNSAFE.sub("_", (user_id or "").strip())[:64]
    return cleaned or DEFAULT_USER_ID


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SessionContext:
    """Everything the broker and service layers need to act for one user."""

    user_id: str
    trading_mode: TradingMode
    portfolio_file: Path
    data_dir: Path
    max_order_value: float
    default_product: str
    default_exchange: str
    paper_starting_cash: float

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
    """Context for an MCP user, isolated in its own directory under the data root."""
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


# --------------------------------------------------------------------------- #
# account record
# --------------------------------------------------------------------------- #
def load_account(ctx: SessionContext) -> dict[str, Any] | None:
    """The user's account record, or None if they have not been set up."""
    if not ctx.account_file.exists():
        return None
    try:
        data = json.loads(ctx.account_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read account record %s: %s", ctx.account_file, exc)
        return None


def save_account(ctx: SessionContext, data: dict[str, Any]) -> dict[str, Any]:
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    data = {**data, "updated_at": _now()}
    ctx.account_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def account_exists(ctx: SessionContext) -> bool:
    return load_account(ctx) is not None


def create_account(
    ctx: SessionContext,
    mode: TradingMode = TradingMode.PAPER,
    starting_cash: float | None = None,
) -> dict[str, Any]:
    """Create (or return) the user's account record and storage directory."""
    existing = load_account(ctx)
    if existing is not None:
        return existing

    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "version": ACCOUNT_VERSION,
        "user_id": ctx.user_id,
        "mode": mode.value,
        "broker": "paper" if mode is TradingMode.PAPER else "groww",
        "starting_cash": float(
            starting_cash if starting_cash is not None else ctx.paper_starting_cash
        ),
        "created_at": _now(),
        "migrated_legacy_portfolio": import_legacy_portfolio(ctx),
    }
    log.info("Created %s account for user %s", mode.value, ctx.user_id)
    return save_account(ctx, record)


def import_legacy_portfolio(ctx: SessionContext) -> bool:
    """Copy the repo-root portfolio.json into a brand-new user directory.

    Opt-in via TRINETRA_IMPORT_LOCAL_PORTFOLIO=1, and off by default: a copy of
    this project can carry someone else's portfolio.json, and a new user must
    never silently inherit another person's positions.

    Never overwrites an existing portfolio; failure is non-fatal.
    """
    if os.getenv("TRINETRA_IMPORT_LOCAL_PORTFOLIO", "").strip().lower() not in (
        "1", "true", "yes", "on"
    ):
        return False

    legacy = PROJECT_ROOT / "portfolio.json"
    target = ctx.portfolio_file
    if target.exists() or not legacy.exists() or legacy.resolve() == target.resolve():
        return False
    try:
        trades = json.loads(legacy.read_text(encoding="utf-8"))
        if not isinstance(trades, list) or not trades:
            return False
        ctx.data_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(trades, indent=2), encoding="utf-8")
        log.info("Imported %d legacy paper trades to %s", len(trades), target)
        return True
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Legacy portfolio import skipped: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# order audit log
# --------------------------------------------------------------------------- #
def audit_order(ctx: SessionContext, event: str, payload: dict[str, Any]) -> None:
    """Append-only record of every order-affecting event.

    Written before an order reaches the broker, so an attempt is on record even
    if execution then fails. Never raises — auditing must not block a trade the
    user has already authorised.
    """
    try:
        ctx.data_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"ts": _now(), "user_id": ctx.user_id, "event": event,
             "mode": ctx.trading_mode.value, **payload},
            default=str,
        )
        with ctx.audit_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        log.warning("Could not write audit record: %s", exc)
