"""The hosted server: token verification, account resolution, and failing closed.

These are the parts where a mistake exposes someone else's brokerage account, so
they are tested against real signed JWTs rather than stubs.
"""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from trinetra import db
from trinetra.config import TradingMode
from trinetra.session import context_for_account
from trinetra_mcp import runtime
from trinetra_mcp.auth import JwtTokenVerifier, TrinetraAccessToken, auth_enabled

ISSUER = "https://idp.example.com/"
AUDIENCE = "https://api.trinetra.test"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem, key.public_key()


@pytest.fixture
def verifier(keypair, monkeypatch):
    _, public_key = keypair
    v = JwtTokenVerifier(issuer=ISSUER, audience=AUDIENCE,
                         jwks_url=f"{ISSUER}.well-known/jwks.json")
    # Serve the public key directly instead of fetching the IdP's JWKS.
    monkeypatch.setattr(
        v._jwks, "get_signing_key_from_jwt",
        lambda token: type("K", (), {"key": public_key})(),
    )
    return v


def make_token(keypair, **overrides) -> str:
    pem, _ = keypair
    now = int(time.time())
    claims = {
        "sub": "auth0|user-1", "iss": ISSUER, "aud": AUDIENCE,
        "iat": now, "exp": now + 3600, "email": "u@example.com",
        "scope": "trade:execute",
    }
    claims.update(overrides)
    return jwt.encode(claims, pem, algorithm="RS256")


async def verify(verifier, token):
    return await verifier.verify_token(token)


# --------------------------------------------------------------------------- #
# token verification
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_valid_token_is_accepted(verifier, keypair):
    result = await verify(verifier, make_token(keypair))
    assert isinstance(result, TrinetraAccessToken)
    assert result.subject == "auth0|user-1"
    assert result.email == "u@example.com"


@pytest.mark.anyio
@pytest.mark.parametrize("claims, why", [
    ({"exp": int(time.time()) - 60}, "expired"),
    ({"aud": "https://someone-elses-api"}, "wrong audience"),
    ({"iss": "https://evil.example.com/"}, "wrong issuer"),
])
async def test_bad_tokens_are_rejected(verifier, keypair, claims, why):
    assert await verify(verifier, make_token(keypair, **claims)) is None, why


@pytest.mark.anyio
async def test_token_signed_by_another_key_is_rejected(verifier):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = other.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = int(time.time())
    forged = jwt.encode(
        {"sub": "attacker", "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 60},
        pem, algorithm="RS256",
    )
    assert await verify(verifier, forged) is None


@pytest.mark.anyio
async def test_garbage_and_missing_claims_are_rejected(verifier, keypair):
    assert await verify(verifier, "not-a-jwt") is None
    # No `sub` — we would have no identity to key an account on.
    assert await verify(verifier, make_token(keypair, sub=None)) is None


@pytest.mark.anyio
async def test_missing_required_scope_is_rejected(verifier, keypair, monkeypatch):
    monkeypatch.setattr(verifier, "required_scopes", ("trade:execute", "admin"))
    assert await verify(verifier, make_token(keypair)) is None


# --------------------------------------------------------------------------- #
# account resolution
# --------------------------------------------------------------------------- #
@pytest.fixture
def hosted_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'h.db').as_posix()}")
    db.reset_engine()
    db.Base.metadata.create_all(db.get_engine())
    yield
    db.reset_engine()


def _token(subject: str):
    return TrinetraAccessToken(
        token="t", client_id="c", scopes=[], expires_at=None,
        subject=subject, email=f"{subject}@example.com",
    )


def test_token_subject_resolves_to_its_own_account(hosted_db, monkeypatch):
    monkeypatch.setattr(runtime, "get_access_token", lambda: _token("auth0|alice"))
    alice = runtime.current_context()

    monkeypatch.setattr(runtime, "get_access_token", lambda: _token("auth0|bob"))
    bob = runtime.current_context()

    assert alice.account_id and bob.account_id
    assert alice.account_id != bob.account_id
    assert alice.trading_mode is TradingMode.PAPER


def test_same_subject_is_stable_across_calls(hosted_db, monkeypatch):
    monkeypatch.setattr(runtime, "get_access_token", lambda: _token("auth0|alice"))
    assert runtime.current_context().account_id == runtime.current_context().account_id


def test_hosted_server_refuses_unauthenticated_calls(hosted_db, monkeypatch):
    """The dangerous case: a database is configured, so we are serving many
    users — an unauthenticated call must never fall back to a shared account."""
    monkeypatch.setattr(runtime, "get_access_token", lambda: None)
    with pytest.raises(runtime.NotAuthenticated):
        runtime.current_context()


def test_local_mode_still_works_without_auth(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TRINETRA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runtime, "get_access_token", lambda: None)
    assert runtime.current_context().user_id == "local"


# --------------------------------------------------------------------------- #
# confirmations survive in the database
# --------------------------------------------------------------------------- #
def _ctx(account_id, user_id="u"):
    return context_for_account(user_id=user_id, account_id=account_id,
                               trading_mode=TradingMode.PAPER, max_order_value=100_000.0)


def test_confirmation_round_trip_via_database(hosted_db):
    _, account_id = db.upsert_user("auth0|alice")
    ctx = _ctx(account_id, "alice")
    order = {"request": {"trading_symbol": "RELIANCE"}, "reference_price": 2500.0}

    token, ttl = runtime.issue_token(ctx, order, {"symbol": "RELIANCE"})
    assert ttl > 0
    pending, error = runtime.redeem_token(ctx, token)
    assert error is None
    assert pending["order"] == order

    replay, error = runtime.redeem_token(ctx, token)
    assert replay is None and error["status"] == "rejected"


def test_confirmation_is_scoped_to_its_account(hosted_db):
    _, account_a = db.upsert_user("auth0|alice")
    _, account_b = db.upsert_user("auth0|bob")

    token, _ = runtime.issue_token(_ctx(account_a, "alice"), {"request": {}}, {})
    stolen, error = runtime.redeem_token(_ctx(account_b, "bob"), token)
    assert stolen is None and error["status"] == "rejected"


def test_only_a_hash_of_the_token_is_stored(hosted_db):
    _, account_id = db.upsert_user("auth0|alice")
    token, _ = runtime.issue_token(_ctx(account_id), {"request": {}}, {})

    with db.session_scope() as session:
        rows = session.scalars(__import__("sqlalchemy").select(db.Confirmation)).all()
    assert len(rows) == 1
    assert rows[0].token_hash != token
    assert token not in rows[0].token_hash


# --------------------------------------------------------------------------- #
# startup guards
# --------------------------------------------------------------------------- #
def test_hosted_startup_refuses_database_without_auth(hosted_db, monkeypatch):
    from trinetra_mcp import server

    monkeypatch.delenv("OAUTH_ISSUER", raising=False)
    monkeypatch.delenv("OAUTH_AUDIENCE", raising=False)
    assert not auth_enabled()
    with pytest.raises(RuntimeError, match="Refusing to start"):
        server._serve_http()


def test_http_app_exposes_mcp_and_health(monkeypatch):
    from trinetra_mcp.server import build_http_app

    monkeypatch.delenv("DATABASE_URL", raising=False)
    paths = {getattr(r, "path", None) for r in build_http_app().routes}
    assert "/mcp" in paths
    assert "/health" in paths


def test_oauth_discovery_is_published_when_configured(monkeypatch):
    from trinetra_mcp.server import build_http_app

    monkeypatch.setenv("OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("OAUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("PUBLIC_URL", "https://trinetra.test")
    paths = {getattr(r, "path", None) for r in build_http_app().routes}
    assert "/.well-known/oauth-protected-resource" in paths, (
        "hosts discover the authorization server through this document"
    )
