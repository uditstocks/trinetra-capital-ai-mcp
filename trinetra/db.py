"""Postgres schema and the database-backed store.

Multi-tenant: every row hangs off a user, and every query is scoped by account,
so one user's data is unreachable from another's session.

Notes on shape:
- `paper_trades` mirrors the JSON trade-log format the paper broker already
  speaks, so the analytics layer needs no changes.
- `audit_events` is hash-chained (each row commits to the previous row's hash)
  so tampering is detectable, and the application role should hold INSERT and
  SELECT only.
- Broker credentials live in `broker_links.secret_ciphertext` and are written
  only by the vault (Phase 2B). Nothing here ever decrypts them.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String,
    create_engine, select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from trinetra.logging_setup import get_logger
from trinetra.session import SessionContext

log = get_logger(__name__)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The identity provider's subject claim — stable, unique, and the only thing
    # we trust to identify a caller.
    subject: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    mode: Mapped[str] = mapped_column(String(16), default="paper")
    broker: Mapped[str] = mapped_column(String(32), default="paper")
    starting_cash: Mapped[float] = mapped_column(Float, default=100_000.0)
    max_order_value: Mapped[float] = mapped_column(Float, default=100_000.0)
    default_product: Mapped[str] = mapped_column(String(8), default="CNC")
    default_exchange: Mapped[str] = mapped_column(String(8), default="NSE")

    # Live trading is a separate, explicit opt-in after a broker is linked.
    live_activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_notional_cap: Mapped[float] = mapped_column(Float, default=100_000.0)
    daily_order_cap: Mapped[int] = mapped_column(Integer, default=20)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class PaperTrade(Base):
    """One simulated fill. Column names mirror the JSON trade-log keys."""

    __tablename__ = "paper_trades"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    timestamp: Mapped[str] = mapped_column(String(40))
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    exchange: Mapped[str] = mapped_column(String(8), default="NSE")
    action: Mapped[str] = mapped_column(String(8))
    product: Mapped[str] = mapped_column(String(8), default="CNC")
    shares: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    total: Mapped[float] = mapped_column(Float)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def to_trade(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "action": self.action,
            "product": self.product,
            "currency": "INR",
            "shares": self.shares,
            "price": self.price,
            "total": self.total,
            "order_id": self.order_id,
            "reference_id": self.reference_id,
            "mode": "paper",
        }


class BrokerLink(Base):
    """A user's link to a real broker. Secrets are written only by the vault."""

    __tablename__ = "broker_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    broker: Mapped[str] = mapped_column(String(32))
    # pending -> awaiting_login (redirect brokers only) -> linked
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # Hash of the one-time link token, kept only while the linking round trip is
    # in flight. Distinct from key_id, which names the vault key that sealed the
    # credentials — conflating the two breaks the redirect callback.
    token_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    # Envelope-encrypted credential blob plus the id of the key that wrapped it,
    # so rotation can find what needs re-wrapping. Never decrypted in this module.
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    linked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Order(Base):
    """A real (live) order we submitted, for reconciliation against the broker."""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    # Doubles as the idempotency key: one submission per reference_id, ever.
    reference_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    broker: Mapped[str] = mapped_column(String(32))
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trading_symbol: Mapped[str] = mapped_column(String(40), index=True)
    exchange: Mapped[str] = mapped_column(String(8))
    transaction_type: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    order_type: Mapped[str] = mapped_column(String(8))
    product: Mapped[str] = mapped_column(String(8))
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="submitted", index=True)
    average_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class AuditEvent(Base):
    """Append-only, hash-chained. Grant the app role INSERT and SELECT only."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    event: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    row_hash: Mapped[str] = mapped_column(String(64))


class Confirmation(Base):
    """A pending order awaiting the user's approval.

    Only a hash of the token is stored, never the token itself — a database leak
    must not let anyone redeem someone's pending order. Rows outlive a restart
    and are visible to every replica, which an in-process dict would not be.
    """

    __tablename__ = "confirmations"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    order: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    preview: Mapped[dict] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def store_confirmation(ctx: SessionContext, token: str, order: dict[str, Any],
                       preview: dict[str, Any], ttl_seconds: float) -> None:
    from datetime import timedelta

    with session_scope() as db:
        db.merge(Confirmation(
            token_hash=token_hash(token),
            account_id=ctx.account_id or "",
            user_id=ctx.user_id,
            order=order,
            preview=preview,
            expires_at=_now() + timedelta(seconds=ttl_seconds),
        ))
        db.commit()


def take_confirmation(ctx: SessionContext, token: str) -> dict[str, Any] | None:
    """Consume a pending confirmation. Returns None if unknown, expired, or
    belonging to another account. Deleted on any matching attempt, so a replay
    can never place a second order."""
    with session_scope() as db:
        row = db.get(Confirmation, token_hash(token))
        if row is None:
            return None
        account_id, user_id = row.account_id, row.user_id
        order, preview, expires_at = row.order, row.preview, row.expires_at
        db.delete(row)
        db.execute(Confirmation.__table__.delete().where(
            Confirmation.expires_at < _now()))
        db.commit()

    if account_id != (ctx.account_id or "") or user_id != ctx.user_id:
        return None
    # Some backends hand back a naive datetime even for a timezone-aware column,
    # so anchor it to UTC before comparing rather than letting it be read as
    # local time — that would make a fresh token look long expired.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= _now():
        return None
    return {"order": order, "preview": preview}


def as_column_datetime(session, value: datetime) -> datetime:
    """Render a datetime the way this backend stores them.

    Postgres keeps the offset on a TIMESTAMPTZ column; SQLite writes naive text.
    Comparing an aware Python value against a naive column matches *nothing* and
    raises no error — which would make a daily trading limit look like it had
    never been reached. Normalise the bound rather than trusting the driver.
    """
    if session.bind.dialect.name == "sqlite":
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def chain_hash(prev_hash: str | None, body: dict[str, Any]) -> str:
    """Hash of this row's content committed to the previous row's hash."""
    material = (prev_hash or "") + json.dumps(body, sort_keys=True, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
_engine = None
_Session: sessionmaker | None = None


def _normalise_url(url: str) -> str:
    """Railway and Heroku hand out `postgres://`, which SQLAlchemy rejects, and
    we pin the psycopg 3 driver explicitly."""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def get_engine():
    global _engine, _Session
    if _engine is None:
        url = os.getenv("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError("DATABASE_URL is not set; the database store is unavailable.")
        _engine = create_engine(
            _normalise_url(url), pool_pre_ping=True, pool_size=5, max_overflow=10
        )
        _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def session_scope():
    if _Session is None:
        get_engine()
    return _Session()


def reset_engine() -> None:
    """Drop the cached engine (tests, and after a config change)."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None


# --------------------------------------------------------------------------- #
# user / account resolution
# --------------------------------------------------------------------------- #
def upsert_user(subject: str, email: str | None = None) -> tuple[str, str]:
    """Find or create the user for an authenticated subject, plus their paper
    account. Returns (user_id, account_id). First login provisions both."""
    with session_scope() as db:
        user = db.scalar(select(User).where(User.subject == subject))
        if user is None:
            user = User(subject=subject, email=email)
            db.add(user)
            db.flush()
            log.info("Provisioned user for subject %s", subject[:12] + "…")
        elif email and user.email != email:
            user.email = email

        account = db.scalar(select(Account).where(Account.user_id == user.id))
        if account is None:
            account = Account(user_id=user.id)
            db.add(account)
            db.flush()
        db.commit()
        return user.id, account.id


def load_account_row(account_id: str) -> Account | None:
    with session_scope() as db:
        return db.get(Account, account_id)


# --------------------------------------------------------------------------- #
# database store
# --------------------------------------------------------------------------- #
class DbStore:
    """Store implementation backed by Postgres, scoped to one account."""

    def __init__(self, ctx: SessionContext) -> None:
        if not ctx.account_id:
            raise RuntimeError(
                "SessionContext has no account_id; the database store requires an "
                "authenticated account."
            )
        self.ctx = ctx
        self.account_id = ctx.account_id

    def load_account(self) -> dict[str, Any] | None:
        row = load_account_row(self.account_id)
        if row is None:
            return None
        return {
            "version": 1,
            "user_id": row.user_id,
            "account_id": row.id,
            "mode": row.mode,
            "broker": row.broker,
            "starting_cash": row.starting_cash,
            "max_order_value": row.max_order_value,
            "live_activated": row.live_activated_at is not None,
            "kill_switch": row.kill_switch,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    def save_account(self, data: dict[str, Any]) -> dict[str, Any]:
        with session_scope() as db:
            row = db.get(Account, self.account_id)
            if row is None:
                raise RuntimeError(f"Account {self.account_id} does not exist.")
            for field in ("mode", "broker", "starting_cash", "max_order_value"):
                if field in data:
                    setattr(row, field, data[field])
            db.commit()
        return self.load_account() or data

    def load_trades(self) -> list[dict[str, Any]]:
        with session_scope() as db:
            rows = db.scalars(
                select(PaperTrade)
                .where(PaperTrade.account_id == self.account_id)
                .order_by(PaperTrade.created_at)
            ).all()
            return [r.to_trade() for r in rows]

    def save_trades(self, trades: list[dict[str, Any]]) -> None:
        """Persist the trade log.

        The paper broker appends to the full list and hands the whole thing back,
        so only rows we have not seen are inserted — the table stays append-only
        rather than being rewritten on every fill. Rows are matched by
        reference_id, which the broker always sets; anything without one is
        skipped rather than risking a duplicate fill.
        """
        with session_scope() as db:
            known = set(db.scalars(
                select(PaperTrade.reference_id).where(
                    PaperTrade.account_id == self.account_id
                )
            ).all())
            for trade in trades:
                ref = trade.get("reference_id")
                if not ref or ref in known:
                    continue
                known.add(ref)
                db.add(PaperTrade(
                    account_id=self.account_id,
                    timestamp=str(trade.get("timestamp", "")),
                    symbol=trade.get("symbol", ""),
                    exchange=trade.get("exchange", "NSE"),
                    action=trade.get("action", ""),
                    product=trade.get("product", "CNC"),
                    shares=int(trade.get("shares", 0)),
                    price=float(trade.get("price", 0)),
                    total=float(trade.get("total", 0)),
                    order_id=trade.get("order_id"),
                    reference_id=ref,
                ))
            db.commit()

    def append_audit_strict(self, event: str, payload: dict[str, Any]) -> None:
        """Write the audit row, letting a failure propagate to the caller."""
        self._write_audit(event, payload)

    def append_audit(self, event: str, payload: dict[str, Any]) -> None:
        try:
            self._write_audit(event, payload)
        except Exception as exc:  # noqa: BLE001 - auditing must never block a trade
            log.error("Could not write audit event %s: %s", event, exc)

    def _write_audit(self, event: str, payload: dict[str, Any]) -> None:
        """Append one hash-chained row. Failures propagate to the caller."""
        with session_scope() as db:
            prev = db.scalar(
                select(AuditEvent)
                .where(AuditEvent.account_id == self.account_id)
                .order_by(AuditEvent.ts.desc())
                .limit(1)
            )
            body = {
                "account_id": self.account_id,
                "user_id": self.ctx.user_id,
                "event": event,
                "mode": self.ctx.trading_mode.value,
                "payload": payload,
            }
            db.add(AuditEvent(
                **body,
                prev_hash=prev.row_hash if prev else None,
                row_hash=chain_hash(prev.row_hash if prev else None, body),
            ))
            db.commit()
