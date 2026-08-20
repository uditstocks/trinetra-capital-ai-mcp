"""Broker links — which real account a user has connected, and its sealed keys.

The credential plaintext never passes through the MCP tool surface. A user is
sent a one-time signed link to a page we host, enters their broker keys there
over TLS, and this module seals them straight into the vault.

Two shapes of broker login are supported from the start, because retrofitting the
second onto the first is where these flows usually go wrong:

    form      the user pastes API credentials (Groww: api_key + TOTP secret)
    redirect  the broker hosts the login and calls us back (Zerodha Kite)

Link tokens are single-use, short-lived, bound to one user, and stored only as a
hash — the same discipline as order confirmations.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from trinetra import vault
from trinetra.logging_setup import get_logger
from trinetra.session import SessionContext

log = get_logger(__name__)

LINK_TTL_MINUTES = 15

# What each broker needs, and how its login is shaped. Adding a broker is an
# entry here plus an adapter — not a change to the linking flow.
BROKERS: dict[str, dict[str, Any]] = {
    "groww": {
        "label": "Groww",
        "flow": "form",
        "fields": [
            {"name": "api_key", "label": "API key", "required": True},
            {"name": "totp_secret", "label": "TOTP secret", "required": False,
             "help": "From Groww's TOTP setup. Leave blank if using an API secret."},
            {"name": "api_secret", "label": "API secret", "required": False,
             "help": "Only for the approval flow. Leave blank if using a TOTP secret."},
        ],
        "help_url": "https://groww.in/trade-api/docs",
    },
    "zerodha": {
        "label": "Zerodha (Kite Connect)",
        "flow": "redirect",
        "fields": [
            {"name": "api_key", "label": "Kite API key", "required": True},
            {"name": "api_secret", "label": "Kite API secret", "required": True},
        ],
        "help_url": "https://kite.trade/docs/connect/v3/",
    },
}


def supported_brokers() -> list[dict[str, str]]:
    return [{"id": key, "label": spec["label"], "flow": spec["flow"]}
            for key, spec in BROKERS.items()]


class LinkError(RuntimeError):
    """A link request that cannot proceed."""


# --------------------------------------------------------------------------- #
# one-time link tokens
# --------------------------------------------------------------------------- #
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _signing_key() -> bytes:
    """Key for the link signature. Falls back to the vault master key so there is
    one fewer secret to configure, but never exposes it."""
    explicit = os.getenv("TRINETRA_LINK_SECRET", "").strip()
    if explicit:
        return explicit.encode("utf-8")
    return hashlib.sha256(next(iter(vault.master_keys().values()))).digest()


def sign_token(token: str) -> str:
    return hmac.new(_signing_key(), token.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def verify_signature(token: str, signature: str) -> bool:
    return hmac.compare_digest(sign_token(token), signature or "")


@dataclass(frozen=True)
class LinkRequest:
    token: str
    signature: str
    broker: str
    expires_at: datetime

    @property
    def query(self) -> str:
        return f"?t={self.token}&s={self.signature}"


def create_link(ctx: SessionContext, broker: str) -> LinkRequest:
    """Record a pending link and return its one-time token.

    Only the token's hash is stored, so a database leak cannot be used to
    complete somebody's broker connection.
    """
    from trinetra import db

    broker = (broker or "").strip().lower()
    if broker not in BROKERS:
        raise LinkError(
            f"Unsupported broker {broker!r}. Supported: {', '.join(BROKERS)}."
        )
    if not vault.is_configured():
        raise LinkError(
            "The credential vault is not configured on this server, so broker "
            "linking is disabled. Set TRINETRA_MASTER_KEY."
        )

    purge_abandoned()
    token = secrets.token_urlsafe(24)
    expires = datetime.now(timezone.utc) + timedelta(minutes=LINK_TTL_MINUTES)

    with db.session_scope() as session:
        # One pending link per broker at a time; a fresh request supersedes it.
        session.execute(
            db.BrokerLink.__table__.delete().where(
                (db.BrokerLink.user_id == ctx.user_id)
                & (db.BrokerLink.broker == broker)
                & (db.BrokerLink.status == "pending")
            )
        )
        session.add(db.BrokerLink(
            user_id=ctx.user_id, broker=broker, status="pending",
            token_hash=_token_hash(token), expires_at=expires,
        ))
        session.commit()

    return LinkRequest(token=token, signature=sign_token(token),
                       broker=broker, expires_at=expires)


def resolve_link(token: str, signature: str) -> dict[str, Any]:
    """Look up a pending link without consuming it, for rendering the form."""
    from trinetra import db

    if not verify_signature(token, signature):
        raise LinkError("This link is not valid.")

    with db.session_scope() as session:
        row = session.scalar(
            select(db.BrokerLink).where(
                (db.BrokerLink.token_hash == _token_hash(token))
                & (db.BrokerLink.status == "pending")
            )
        )
        if row is None:
            raise LinkError("This link has already been used, or never existed.")
        expires_at = row.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            raise LinkError(
                f"This link expired. Ask Trinetra for a new one — links last "
                f"{LINK_TTL_MINUTES} minutes."
            )
        return {"id": row.id, "user_id": row.user_id, "broker": row.broker,
                "spec": BROKERS[row.broker]}


def complete_link(token: str, signature: str, credentials: dict[str, str]) -> str:
    """Seal the submitted credentials against the pending link.

    Returns the broker id. The plaintext is sealed immediately and never reaches
    a log, a response body, or an exception message.
    """
    from trinetra import db

    pending = resolve_link(token, signature)
    spec = pending["spec"]

    cleaned = {k: v.strip() for k, v in credentials.items() if v and v.strip()}
    missing = [f["name"] for f in spec["fields"]
               if f.get("required") and not cleaned.get(f["name"])]
    if missing:
        raise LinkError(f"Missing required field(s): {', '.join(missing)}.")
    if pending["broker"] == "groww" and not (
        cleaned.get("totp_secret") or cleaned.get("api_secret")
    ):
        raise LinkError("Groww needs either a TOTP secret or an API secret.")

    ciphertext, key_id = vault.encrypt(cleaned)

    with db.session_scope() as session:
        row = session.get(db.BrokerLink, pending["id"])
        if row is None or row.status != "pending":
            raise LinkError("This link has already been used.")
        # Retire any earlier link for this user and broker. Without this, a
        # re-link leaves two "linked" rows and lookups can keep returning the
        # superseded credentials — a rotated key would never take effect.
        session.execute(
            db.BrokerLink.__table__.delete().where(
                (db.BrokerLink.user_id == row.user_id)
                & (db.BrokerLink.broker == row.broker)
                & (db.BrokerLink.id != row.id)
                & (db.BrokerLink.status.in_(("linked", "awaiting_login")))
            )
        )
        row.secret_ciphertext = ciphertext
        row.key_id = key_id
        if spec["flow"] == "redirect":
            # Credentials are sealed, but the broker still has to authorise the
            # session. Not usable — and not reported as linked — until then.
            # Keep the expiry running: a login the user abandons must not leave
            # their API key and secret sitting in the database indefinitely.
            row.status = "awaiting_login"
            row.expires_at = datetime.now(timezone.utc) + timedelta(
                minutes=LINK_TTL_MINUTES
            )
        else:
            row.status = "linked"
            row.linked_at = datetime.now(timezone.utc)
            row.token_hash = None
            row.expires_at = None
        session.commit()

    # The cached adapter still holds a session built from the previous secrets.
    _drop_cached_adapter(pending["user_id"], pending["broker"])

    # Field names only — never a value, never a length.
    log.info("Linked %s for user %s (fields: %s)",
             pending["broker"], pending["user_id"], sorted(cleaned))
    return pending["broker"]


def _drop_cached_adapter(user_id: str, broker: str) -> None:
    """Invalidate any live broker built from the credentials we just replaced."""
    try:
        from trinetra.broker import registry

        registry.forget_user(user_id, broker)
    except Exception as exc:  # noqa: BLE001 - linking must not fail over a cache
        log.warning("Could not invalidate cached broker adapter: %s", exc)


# --------------------------------------------------------------------------- #
# reading and removing links
# --------------------------------------------------------------------------- #
def active_link(ctx: SessionContext) -> dict[str, Any] | None:
    """The user's linked broker, described without revealing anything sealed."""
    from trinetra import db

    with db.session_scope() as session:
        row = session.scalar(
            select(db.BrokerLink)
            .where((db.BrokerLink.user_id == ctx.user_id)
                   & (db.BrokerLink.status == "linked"))
            .order_by(db.BrokerLink.linked_at.desc())
            .limit(1)
        )
        if row is None:
            return None
        return {"broker": row.broker, "label": BROKERS[row.broker]["label"],
                "linked_at": row.linked_at.isoformat() if row.linked_at else None}


def load_credentials(ctx: SessionContext, broker: str) -> vault.SecretBundle:
    """Open the sealed credentials for a broker call. The only decryption site."""
    from trinetra import db

    with db.session_scope() as session:
        row = session.scalar(
            select(db.BrokerLink)
            .where((db.BrokerLink.user_id == ctx.user_id)
                   & (db.BrokerLink.broker == broker)
                   & (db.BrokerLink.status == "linked"))
            # Newest wins, so a stale row can never shadow a fresh credential.
            .order_by(db.BrokerLink.linked_at.desc())
            .limit(1)
        )
        if row is None or not row.secret_ciphertext:
            raise LinkError(
                f"No {broker} account is linked. Use link_broker to connect one."
            )
        ciphertext, key_id = row.secret_ciphertext, row.key_id
    return vault.decrypt(ciphertext, key_id)


def unlink(ctx: SessionContext, broker: str | None = None) -> int:
    """Delete stored credentials. Returns how many links were removed."""
    from trinetra import db

    with db.session_scope() as session:
        query = db.BrokerLink.__table__.delete().where(
            db.BrokerLink.user_id == ctx.user_id
        )
        if broker:
            query = query.where(db.BrokerLink.broker == broker.strip().lower())
        removed = session.execute(query).rowcount or 0
        session.commit()
    if removed:
        log.info("Removed %d broker link(s) for user %s", removed, ctx.user_id)
    return removed


def rotate_all() -> int:
    """Re-seal every stored credential under the active master key."""
    from trinetra import db

    target = vault.active_key_id()
    rotated = 0
    with db.session_scope() as session:
        rows = session.scalars(
            select(db.BrokerLink).where(db.BrokerLink.status == "linked")
        ).all()
        for row in rows:
            if not row.secret_ciphertext or row.key_id == target:
                continue
            row.secret_ciphertext, row.key_id = vault.rewrap(
                row.secret_ciphertext, row.key_id
            )
            rotated += 1
        session.commit()
    log.info("Rotated %d credential record(s) onto master key %s", rotated, target)
    return rotated


# --------------------------------------------------------------------------- #
# redirect-login round trip (Zerodha Kite)
# --------------------------------------------------------------------------- #
def load_credentials_by_id(link_id: str) -> vault.SecretBundle:
    """Open a specific link's credentials, for use inside the linking flow."""
    from trinetra import db

    with db.session_scope() as session:
        row = session.get(db.BrokerLink, link_id)
        if row is None or not row.secret_ciphertext:
            raise LinkError("This connection is no longer available.")
        ciphertext, key_id = row.secret_ciphertext, row.key_id
    return vault.decrypt(ciphertext, key_id)


def purge_abandoned() -> int:
    """Delete links whose login was never completed, and their sealed secrets.

    An abandoned Kite login leaves an api_key and api_secret encrypted at rest
    with nothing pointing at them. They are of no use to the user and are only a
    liability to us, so they do not outlive the link's own expiry.
    """
    from trinetra import db

    with db.session_scope() as session:
        cutoff = db.as_column_datetime(session, datetime.now(timezone.utc))
        removed = session.execute(
            db.BrokerLink.__table__.delete().where(
                db.BrokerLink.status.in_(("pending", "awaiting_login"))
                & (db.BrokerLink.expires_at.is_not(None))
                & (db.BrokerLink.expires_at < cutoff)
            )
        ).rowcount or 0
        session.commit()
    if removed:
        log.info("Purged %d abandoned broker link(s)", removed)
    return removed


def linked_record(token: str, signature: str, broker: str) -> dict[str, Any]:
    """Find the in-flight link a redirect callback belongs to."""
    from trinetra import db

    if not verify_signature(token, signature):
        raise LinkError("This link is not valid.")

    purge_abandoned()
    with db.session_scope() as session:
        row = session.scalar(
            select(db.BrokerLink).where(
                (db.BrokerLink.token_hash == _token_hash(token))
                & (db.BrokerLink.broker == broker)
                & (db.BrokerLink.status == "awaiting_login")
            )
        )
        if row is None:
            raise LinkError(
                "This connection has already been completed, or the link expired."
            )
        return {"id": row.id, "user_id": row.user_id, "broker": row.broker}


def attach_access_token(link_id: str, access_token: str) -> None:
    """Fold a freshly issued broker session token into the sealed bundle.

    Re-seals the whole bundle so the access token gets the same protection as the
    keys, and retires the link token now the round trip is done.
    """
    from trinetra import db

    with db.session_scope() as session:
        row = session.get(db.BrokerLink, link_id)
        if row is None or not row.secret_ciphertext:
            raise LinkError("This connection is no longer available.")
        opened = vault.decrypt(row.secret_ciphertext, row.key_id).reveal()
        opened["access_token"] = access_token
        row.secret_ciphertext, row.key_id = vault.encrypt(opened)
        opened.clear()
        row.status = "linked"
        row.linked_at = datetime.now(timezone.utc)
        row.token_hash = None
        row.expires_at = None
        user_id, broker = row.user_id, row.broker
        session.commit()

    # A fresh Kite session must replace the expired one immediately, not in five
    # minutes — this is the daily re-link path.
    _drop_cached_adapter(user_id, broker)
    log.info("Completed redirect login for link %s", link_id)
