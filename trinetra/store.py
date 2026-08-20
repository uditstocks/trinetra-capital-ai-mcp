"""Persistence for account state, paper trades and the audit log.

Two backends behind one protocol:

    FileStore   local stdio server — JSON files under the user's data dir
    DbStore     hosted server — Postgres, multi-tenant

`get_store(ctx)` picks by configuration, so nothing above this layer knows which
is active and the local dev loop keeps working with no database installed.

A store instance is bound to exactly one account.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from trinetra.config import PROJECT_ROOT, TradingMode
from trinetra.logging_setup import get_logger
from trinetra.session import SessionContext

log = get_logger(__name__)

ACCOUNT_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@runtime_checkable
class Store(Protocol):
    """Everything one account's data needs. Implementations are per-account."""

    def load_account(self) -> dict[str, Any] | None: ...
    def save_account(self, data: dict[str, Any]) -> dict[str, Any]: ...
    def load_trades(self) -> list[dict[str, Any]]: ...
    def save_trades(self, trades: list[dict[str, Any]]) -> None: ...
    def append_audit(self, event: str, payload: dict[str, Any]) -> None: ...
    def append_audit_strict(self, event: str, payload: dict[str, Any]) -> None: ...


# --------------------------------------------------------------------------- #
# file backend
# --------------------------------------------------------------------------- #
class FileStore:
    """JSON files under the context's data directory. Used by the local server."""

    def __init__(self, ctx: SessionContext) -> None:
        self.ctx = ctx

    def _ensure_dir(self) -> None:
        self.ctx.data_dir.mkdir(parents=True, exist_ok=True)

    def load_account(self) -> dict[str, Any] | None:
        path = self.ctx.account_file
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read account record %s: %s", path, exc)
            return None

    def save_account(self, data: dict[str, Any]) -> dict[str, Any]:
        self._ensure_dir()
        data = {**data, "updated_at": utc_now()}
        self.ctx.account_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data

    def load_trades(self) -> list[dict[str, Any]]:
        path = self.ctx.portfolio_file
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read %s: %s", path, exc)
            return []

    def save_trades(self, trades: list[dict[str, Any]]) -> None:
        self.ctx.portfolio_file.parent.mkdir(parents=True, exist_ok=True)
        self.ctx.portfolio_file.write_text(json.dumps(trades, indent=2), encoding="utf-8")

    def append_audit(self, event: str, payload: dict[str, Any]) -> None:
        try:
            self._ensure_dir()
            line = json.dumps(
                {"ts": utc_now(), "user_id": self.ctx.user_id, "event": event,
                 "mode": self.ctx.trading_mode.value, **payload},
                default=str,
            )
            with self.ctx.audit_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            log.warning("Could not write audit record: %s", exc)


# --------------------------------------------------------------------------- #
# backend selection
# --------------------------------------------------------------------------- #
def database_url() -> str | None:
    """Postgres URL when the hosted backend is configured, else None."""
    return (os.getenv("DATABASE_URL") or "").strip() or None


def get_store(ctx: SessionContext) -> Store:
    if database_url() is None:
        return FileStore(ctx)
    # Imported lazily so the local server never needs a Postgres driver.
    from trinetra.db import DbStore

    return DbStore(ctx)


# --------------------------------------------------------------------------- #
# account API — the surface callers use
# --------------------------------------------------------------------------- #
def load_account(ctx: SessionContext) -> dict[str, Any] | None:
    return get_store(ctx).load_account()


def account_exists(ctx: SessionContext) -> bool:
    return load_account(ctx) is not None


def save_account(ctx: SessionContext, data: dict[str, Any]) -> dict[str, Any]:
    return get_store(ctx).save_account(data)


def create_account(
    ctx: SessionContext,
    mode: TradingMode = TradingMode.PAPER,
    starting_cash: float | None = None,
) -> dict[str, Any]:
    """Create (or return) this user's account record."""
    store = get_store(ctx)
    existing = store.load_account()
    if existing is not None:
        return existing

    record = {
        "version": ACCOUNT_VERSION,
        "user_id": ctx.user_id,
        "mode": mode.value,
        "broker": "paper" if mode is TradingMode.PAPER else "groww",
        "starting_cash": float(
            starting_cash if starting_cash is not None else ctx.paper_starting_cash
        ),
        "created_at": utc_now(),
        "migrated_legacy_portfolio": import_legacy_portfolio(ctx),
    }
    log.info("Created %s account for user %s", mode.value, ctx.user_id)
    return store.save_account(record)


def audit_order(ctx: SessionContext, event: str, payload: dict[str, Any]) -> None:
    """Append-only record of an order-affecting event. Never raises.

    Used for everything except the live submission itself, where losing the
    record matters more than losing the event — see `audit_order_strict`.
    """
    get_store(ctx).append_audit(event, payload)


def audit_order_strict(ctx: SessionContext, event: str, payload: dict[str, Any]) -> None:
    """Audit that must succeed, or the caller must not proceed.

    For a real-money submission the trade-off flips: an unrecorded live order is
    worse than a refused one, because nobody can later say what happened.
    """
    store = get_store(ctx)
    strict = getattr(store, "append_audit_strict", None)
    if strict is not None:
        strict(event, payload)
    else:
        store.append_audit(event, payload)


def import_legacy_portfolio(ctx: SessionContext) -> bool:
    """Seed a brand-new account from the repo-root portfolio.json.

    Opt-in via TRINETRA_IMPORT_LOCAL_PORTFOLIO=1, and off by default: a copy of
    this project can carry someone else's portfolio.json, and a new user must
    never silently inherit another person's positions. Local use only — it is a
    developer convenience, not a hosted feature.
    """
    if os.getenv("TRINETRA_IMPORT_LOCAL_PORTFOLIO", "").strip().lower() not in (
        "1", "true", "yes", "on"
    ):
        return False
    if database_url() is not None:
        return False

    legacy = PROJECT_ROOT / "portfolio.json"
    target = ctx.portfolio_file
    if target.exists() or not legacy.exists() or legacy.resolve() == target.resolve():
        return False
    try:
        trades = json.loads(legacy.read_text(encoding="utf-8"))
        if not isinstance(trades, list) or not trades:
            return False
        get_store(ctx).save_trades(trades)
        log.info("Imported %d legacy paper trades for %s", len(trades), ctx.user_id)
        return True
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Legacy portfolio import skipped: %s", exc)
        return False
