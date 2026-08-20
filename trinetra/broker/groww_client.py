"""Groww SDK session management: authentication + daily access-token caching.

Groww access tokens expire daily, so we cache the token to disk keyed by the
calendar date and only re-authenticate when the cached token is stale or a call
returns 401. This keeps unattended/scheduled runs from re-authing on every call
while still rotating the token each day.

The actual `GrowwAPI` instance is created lazily and reused. Import errors and
auth errors are surfaced as `BrokerError` with actionable messages.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from trinetra.config import AuthMethod, settings
from trinetra.broker.base import BrokerError
from trinetra.logging_setup import get_logger

log = get_logger(__name__)

_client: Any = None  # cached GrowwAPI instance


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load_cached_token() -> str | None:
    path: Path = settings.token_cache_file
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("date") != _today():
        return None
    if data.get("auth_method") != settings.auth_method.value:
        return None
    return data.get("access_token")


def _save_cached_token(token: str) -> None:
    try:
        settings.token_cache_file.write_text(
            json.dumps(
                {
                    "access_token": token,
                    "date": _today(),
                    "auth_method": settings.auth_method.value,
                    "created_at": datetime.now().isoformat(),
                }
            )
        )
        # Best effort: tighten permissions where the OS supports it.
        try:
            settings.token_cache_file.chmod(0o600)
        except (OSError, NotImplementedError):
            pass
    except OSError as exc:  # caching is an optimisation, never fatal
        log.debug("Could not cache Groww token: %s", exc)


def generate_access_token() -> str:
    """Authenticate with Groww from process configuration and return a token.

    Single-user only — on a hosted server this would be the operator's account.
    """
    _refuse_global_on_hosted()
    try:
        from growwapi import GrowwAPI
    except ImportError as exc:
        raise BrokerError(
            "The 'growwapi' package is not installed. Run: pip install growwapi pyotp"
        ) from exc

    method = settings.auth_method
    if method is AuthMethod.NONE:
        raise BrokerError(
            "No Groww credentials configured. Set GROWW_API_KEY and either "
            "GROWW_TOTP_SECRET (TOTP flow) or GROWW_API_SECRET (approval flow). "
            "Run `python connect_groww.py` for guided setup."
        )

    try:
        if method is AuthMethod.TOTP:
            import pyotp

            totp = pyotp.TOTP(settings.groww_totp_secret).now()
            log.info("Authenticating with Groww via TOTP flow…")
            return GrowwAPI.get_access_token(api_key=settings.groww_api_key, totp=totp)

        log.info("Authenticating with Groww via API-key/secret approval flow…")
        return GrowwAPI.get_access_token(
            api_key=settings.groww_api_key, secret=settings.groww_api_secret
        )
    except BrokerError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalise any SDK/HTTP error
        raise BrokerError(f"Groww authentication failed: {exc}") from exc


def build_client(credentials, force_refresh: bool = False):
    """An authenticated GrowwAPI for one user's own credentials.

    The hosted path calls this with the bundle opened from the vault, so no
    global config is consulted and two users never share a session. Tokens are
    not cached to disk here — that cache is keyed by process, not by user, and
    would leak one account's session into another's.
    """
    try:
        from growwapi import GrowwAPI
    except ImportError as exc:
        raise BrokerError(
            "The 'growwapi' package is not installed. Run: pip install growwapi pyotp"
        ) from exc

    api_key = credentials.require("api_key")
    totp_secret = credentials.get("totp_secret")
    api_secret = credentials.get("api_secret")
    try:
        if totp_secret:
            import pyotp

            token = GrowwAPI.get_access_token(
                api_key=api_key, totp=pyotp.TOTP(totp_secret).now()
            )
        elif api_secret:
            token = GrowwAPI.get_access_token(api_key=api_key, secret=api_secret)
        else:
            raise BrokerError(
                "Stored Groww credentials have neither a TOTP secret nor an API secret."
            )
    except BrokerError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalise SDK/HTTP errors, never echo creds
        raise BrokerError(f"Groww authentication failed: {type(exc).__name__}") from None
    return GrowwAPI(token)


def _refuse_global_on_hosted() -> None:
    """The process-global client must not exist on a multi-tenant server.

    It authenticates from environment configuration — the operator's own broker
    account — and caches the session in a module global and a shared file. On a
    server handling many users that is one account's session leaking into
    everyone's requests. Hosted code paths build a client per user instead.
    """
    from trinetra import store

    if store.database_url() is not None:
        raise BrokerError(
            "Refusing to use server-wide Groww credentials on a multi-user "
            "deployment. Each user's broker session is built from their own "
            "linked credentials."
        )


def get_client(force_refresh: bool = False):
    """Return a ready-to-use, authenticated GrowwAPI instance (cached).

    Single-user only — see _refuse_global_on_hosted.
    """
    _refuse_global_on_hosted()
    global _client
    if _client is not None and not force_refresh:
        return _client

    try:
        from growwapi import GrowwAPI
    except ImportError as exc:
        raise BrokerError(
            "The 'growwapi' package is not installed. Run: pip install growwapi pyotp"
        ) from exc

    token = None if force_refresh else _load_cached_token()
    if token is None:
        token = generate_access_token()
        _save_cached_token(token)
    else:
        log.debug("Reusing cached Groww access token for %s.", _today())

    _client = GrowwAPI(token)
    return _client


def reset_client() -> None:
    """Drop the cached client + token (used on 401 to force a re-auth)."""
    global _client
    _client = None
    try:
        settings.token_cache_file.unlink(missing_ok=True)
    except OSError:
        pass
