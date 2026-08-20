"""The broker-linking web routes.

Mounted into the same ASGI app as the MCP endpoint, so one deployment serves
both. These are the only routes that ever see credential plaintext, and it
travels exactly one path: browser form → vault → database.

Kite's redirect flow needs the link token carried through Zerodha and back, which
`redirect_params` does. If a broker ever fails to return it, the callback refuses
rather than guessing which account the login belonged to.
"""

from __future__ import annotations

from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from trinetra import brokerlink, vault
from trinetra.logging_setup import get_logger
from trinetra_web import pages

log = get_logger(__name__)

NO_STORE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # The page loads nothing external; say so, so a proxy cannot inject anything.
    "Content-Security-Policy":
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'",
}


def _html(markup: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(markup, status_code=status, headers=NO_STORE)


def _failure(detail: str, status: int = 400) -> HTMLResponse:
    return _html(pages.result_page(False, "This link cannot be used", detail), status)


async def link_form(request):
    """Render the credential form for a pending link."""
    token = request.query_params.get("t", "")
    signature = request.query_params.get("s", "")
    try:
        pending = brokerlink.resolve_link(token, signature)
    except brokerlink.LinkError as exc:
        return _failure(str(exc))

    action = f"/link/submit?t={token}&s={signature}"
    return _html(pages.link_form(pending["broker"], pending["spec"], action))


async def link_submit(request):
    """Receive credentials and seal them.

    Groww is complete at this point. Zerodha still needs the user to authorise at
    Kite, so it is sealed and then redirected onward.
    """
    token = request.query_params.get("t", "")
    signature = request.query_params.get("s", "")
    form = await request.form()
    submitted = {k: str(v) for k, v in form.items()}

    try:
        pending = brokerlink.resolve_link(token, signature)
        broker = brokerlink.complete_link(token, signature, submitted)
    except brokerlink.LinkError as exc:
        return _failure(str(exc))
    except vault.VaultError as exc:
        log.error("Vault refused a credential write: %s", exc)
        return _failure("The server could not store these credentials securely.", 500)
    finally:
        submitted.clear()  # drop the plaintext as soon as it is no longer needed

    if broker == "zerodha":
        return await _start_kite_login(pending, token, signature)

    return _html(pages.result_page(
        True, "Connected",
        f"Your {brokerlink.BROKERS[broker]['label']} account is linked. Trading is "
        "still in paper mode until you activate live trading.",
    ))


async def _start_kite_login(pending: dict, token: str, signature: str):
    """Send the user to Kite, carrying the link token through the round trip."""
    from trinetra.broker import zerodha_broker

    try:
        credentials = brokerlink.load_credentials_by_id(pending["id"])
        api_key = credentials.require("api_key")
        url = zerodha_broker.login_url(api_key)
    except Exception as exc:  # noqa: BLE001 - never surface credential detail
        log.error("Could not start Kite login: %s", type(exc).__name__)
        return _failure("Zerodha login could not be started. Check your API key.")

    # Kite appends redirect_params verbatim to its callback, which is how the
    # callback knows whose login it is completing.
    joiner = "&" if "?" in url else "?"
    carried = f"t%3D{token}%26s%3D{signature}"
    return RedirectResponse(f"{url}{joiner}redirect_params={carried}", status_code=303,
                            headers=NO_STORE)


async def kite_callback(request):
    """Exchange Kite's request_token for a daily access token and seal it."""
    from trinetra.broker import zerodha_broker

    params = request.query_params
    if params.get("status") == "cancelled":
        return _failure("You cancelled the Zerodha login.")

    token = params.get("t", "")
    signature = params.get("s", "")
    request_token = params.get("request_token", "")
    if not (token and signature and request_token):
        return _failure(
            "Zerodha did not return enough information to identify this connection. "
            "Ask Trinetra for a fresh link and try again."
        )

    try:
        link = brokerlink.linked_record(token, signature, "zerodha")
        credentials = brokerlink.load_credentials_by_id(link["id"])
        session = zerodha_broker.exchange_request_token(
            credentials.require("api_key"),
            credentials.require("api_secret"),
            request_token,
        )
        brokerlink.attach_access_token(link["id"], session["access_token"])
    except brokerlink.LinkError as exc:
        return _failure(str(exc))
    except Exception as exc:  # noqa: BLE001 - never echo credential material
        log.error("Kite token exchange failed: %s", type(exc).__name__)
        return _failure("Zerodha rejected the login. Ask Trinetra for a fresh link.")

    return _html(pages.result_page(
        True, "Connected",
        "Your Zerodha account is linked. Kite sessions expire daily, so Trinetra "
        "will ask you to reconnect each trading day. Trading stays in paper mode "
        "until you activate live trading.",
    ))


ROUTES = [
    Route("/link", link_form, methods=["GET"]),
    Route("/link/submit", link_submit, methods=["POST"]),
    Route("/link/zerodha/callback", kite_callback, methods=["GET"]),
]
