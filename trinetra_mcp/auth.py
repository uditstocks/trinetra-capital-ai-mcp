"""OAuth for the hosted server.

Trinetra is an OAuth **Resource Server**, not an Authorization Server. Login and
token issuance are delegated to an identity provider; here we only verify the
access tokens it signs. Running our own authorization server would be a large
security surface for no product gain.

Configuration (all from the environment, none of it secret):

    OAUTH_ISSUER      https://your-tenant.example.com/     the IdP
    OAUTH_AUDIENCE    the API identifier this server accepts
    OAUTH_JWKS_URL    optional; defaults to <issuer>/.well-known/jwks.json
    PUBLIC_URL        this server's own https URL
    OAUTH_SCOPES      optional space-separated scopes a token must carry

Verification is deliberately strict: signature, issuer, audience and expiry are
all checked, and an unverifiable token is simply rejected — never trusted with a
warning.
"""

from __future__ import annotations

import os

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from trinetra.logging_setup import get_logger

log = get_logger(__name__)


class TrinetraAccessToken(AccessToken):
    """AccessToken plus the identity claims we key accounts on.

    The base model carries the OAuth *client*; we need the *user*, so the verified
    subject and email ride along here.
    """

    subject: str
    email: str | None = None


class JwtTokenVerifier(TokenVerifier):
    """Verifies IdP-issued JWTs against the issuer's published signing keys."""

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        required_scopes: tuple[str, ...] = (),
    ) -> None:
        self.issuer = issuer.rstrip("/") + "/"
        self.audience = audience
        self.required_scopes = required_scopes
        self._jwks = PyJWKClient(
            jwks_url or f"{self.issuer}.well-known/jwks.json",
            cache_keys=True,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "RS512", "ES256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            # Expired, wrong audience, wrong issuer, bad signature, missing claim.
            log.info("Rejected access token: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 - JWKS fetch failures must not 500
            log.warning("Token verification unavailable: %s", exc)
            return None

        scopes = _scopes(claims)
        missing = [s for s in self.required_scopes if s not in scopes]
        if missing:
            log.info("Rejected access token: missing scopes %s", missing)
            return None

        return TrinetraAccessToken(
            token=token,
            client_id=claims.get("azp") or claims.get("client_id") or claims["sub"],
            scopes=scopes,
            expires_at=claims.get("exp"),
            subject=claims["sub"],
            email=claims.get("email"),
        )


def _scopes(claims: dict) -> list[str]:
    """Scopes appear as a space-delimited string or a list, depending on the IdP."""
    raw = claims.get("scope") or claims.get("scp") or []
    return raw.split() if isinstance(raw, str) else list(raw)


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #
def auth_enabled() -> bool:
    return bool(os.getenv("OAUTH_ISSUER") and os.getenv("OAUTH_AUDIENCE"))


def build_auth() -> tuple[JwtTokenVerifier, AuthSettings] | tuple[None, None]:
    """Verifier and settings when OAuth is configured, else (None, None).

    Returning None is what keeps the local stdio server running unauthenticated;
    the hosted server refuses to start without this (see server.py).
    """
    if not auth_enabled():
        return None, None

    issuer = os.environ["OAUTH_ISSUER"]
    audience = os.environ["OAUTH_AUDIENCE"]
    public_url = os.getenv("PUBLIC_URL", "").rstrip("/")
    if not public_url:
        raise RuntimeError("PUBLIC_URL must be set when OAuth is enabled.")

    scopes = tuple(os.getenv("OAUTH_SCOPES", "").split())
    verifier = JwtTokenVerifier(
        issuer=issuer,
        audience=audience,
        jwks_url=os.getenv("OAUTH_JWKS_URL") or None,
        required_scopes=scopes,
    )
    settings = AuthSettings(
        issuer_url=AnyHttpUrl(issuer),
        resource_server_url=AnyHttpUrl(public_url),
        required_scopes=list(scopes) or None,
    )
    log.info("OAuth enabled — issuer %s, audience %s", issuer, audience)
    return verifier, settings
