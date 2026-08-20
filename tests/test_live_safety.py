"""The money path: the vault, broker linking, and every gate before a real order.

This is the file that matters most. A defect here does not produce a wrong number
on a screen — it exposes someone's brokerage account or places an order they did
not authorise. Each test states the specific harm it is preventing.
"""
from __future__ import annotations

import base64
import json
import pickle
from datetime import datetime, timezone

import pytest

from trinetra import brokerlink, db, limits, vault
from trinetra.config import TradingMode
from trinetra.session import context_for_account

MASTER_A = base64.b64encode(b"A" * 32).decode()
MASTER_B = base64.b64encode(b"B" * 32).decode()
CREDS = {"api_key": "gw_live_key_123", "totp_secret": "JBSWY3DPEHPK3PXP"}


@pytest.fixture
def vault_key(monkeypatch):
    monkeypatch.setenv("TRINETRA_MASTER_KEY", MASTER_A)
    monkeypatch.delenv("TRINETRA_MASTER_KEYS", raising=False)
    monkeypatch.delenv("TRINETRA_ACTIVE_KEY", raising=False)


@pytest.fixture
def hosted(tmp_path, monkeypatch, vault_key):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'live.db').as_posix()}")
    monkeypatch.delenv(limits.GLOBAL_KILL_ENV, raising=False)
    db.reset_engine()
    db.Base.metadata.create_all(db.get_engine())
    user_id, account_id = db.upsert_user("auth0|trader")
    yield context_for_account(user_id=user_id, account_id=account_id,
                              trading_mode=TradingMode.PAPER, max_order_value=100_000.0)
    db.reset_engine()


def _link_groww(ctx) -> None:
    request = brokerlink.create_link(ctx, "groww")
    brokerlink.complete_link(request.token, request.signature, dict(CREDS))


# =========================================================================== #
# Vault — a database leak must not yield usable credentials
# =========================================================================== #
def test_credentials_round_trip(vault_key):
    ciphertext, key_id = vault.encrypt(CREDS)
    assert vault.decrypt(ciphertext, key_id).reveal() == CREDS


def test_ciphertext_does_not_contain_the_plaintext(vault_key):
    ciphertext, _ = vault.encrypt(CREDS)
    for secret in CREDS.values():
        assert secret.encode() not in ciphertext


@pytest.mark.parametrize("position", [0, 20, -1])
def test_tampered_ciphertext_is_refused(vault_key, position):
    """AES-GCM authenticates: a modified record must fail, not decrypt to
    something an attacker chose."""
    ciphertext, key_id = vault.encrypt(CREDS)
    corrupted = bytearray(ciphertext)
    corrupted[position] ^= 0xFF
    with pytest.raises(vault.VaultError):
        vault.decrypt(bytes(corrupted), key_id)


def test_another_master_key_cannot_open_it(vault_key, monkeypatch):
    ciphertext, key_id = vault.encrypt(CREDS)
    monkeypatch.setenv("TRINETRA_MASTER_KEY", MASTER_B)
    with pytest.raises(vault.VaultError):
        vault.decrypt(ciphertext, key_id)


def test_missing_master_key_is_a_hard_error(monkeypatch):
    monkeypatch.delenv("TRINETRA_MASTER_KEY", raising=False)
    monkeypatch.delenv("TRINETRA_MASTER_KEYS", raising=False)
    assert vault.is_configured() is False
    with pytest.raises(vault.VaultError):
        vault.encrypt(CREDS)


def test_rotation_rewraps_without_losing_old_records(monkeypatch):
    monkeypatch.setenv("TRINETRA_MASTER_KEYS", f"v1:{MASTER_A}")
    monkeypatch.setenv("TRINETRA_ACTIVE_KEY", "v1")
    old_ciphertext, old_key = vault.encrypt(CREDS)

    # A second key is introduced and becomes active.
    monkeypatch.setenv("TRINETRA_MASTER_KEYS", f"v1:{MASTER_A},v2:{MASTER_B}")
    monkeypatch.setenv("TRINETRA_ACTIVE_KEY", "v2")
    assert vault.decrypt(old_ciphertext, old_key).reveal() == CREDS, "old records stay readable"

    new_ciphertext, new_key = vault.rewrap(old_ciphertext, old_key)
    assert new_key == "v2"
    assert vault.decrypt(new_ciphertext, new_key).reveal() == CREDS


# --------------------------------------------------------------------------- #
# SecretBundle — plaintext must not escape by accident
# --------------------------------------------------------------------------- #
def test_secret_bundle_never_renders_its_values():
    bundle = vault.SecretBundle(CREDS)
    rendered = [repr(bundle), str(bundle), f"{bundle}", "{}".format(bundle),
                f"credentials={bundle!r}", f"{bundle!s}"]
    for text in rendered:
        for secret in CREDS.values():
            assert secret not in text, f"leaked through {text!r}"
    assert "api_key" in repr(bundle), "field names are fine — values are not"


def test_secret_bundle_cannot_be_serialised():
    """Pickling or JSON-dumping a bundle would write plaintext to a log or queue."""
    bundle = vault.SecretBundle(CREDS)
    with pytest.raises(TypeError):
        pickle.dumps(bundle)
    with pytest.raises(TypeError):
        json.dumps(bundle)
    with pytest.raises(TypeError):
        list(bundle)


def test_secret_bundle_does_not_leak_through_a_traceback():
    bundle = vault.SecretBundle(CREDS)
    try:
        raise RuntimeError(f"failed with {bundle}")
    except RuntimeError as exc:
        assert CREDS["api_key"] not in str(exc)


# =========================================================================== #
# Broker linking
# =========================================================================== #
def test_link_seals_credentials_and_reveals_nothing(hosted):
    _link_groww(hosted)
    link = brokerlink.active_link(hosted)
    assert link["broker"] == "groww"
    assert CREDS["api_key"] not in json.dumps(link)

    opened = brokerlink.load_credentials(hosted, "groww")
    assert opened.reveal() == CREDS


def test_link_token_is_single_use(hosted):
    request = brokerlink.create_link(hosted, "groww")
    brokerlink.complete_link(request.token, request.signature, dict(CREDS))
    with pytest.raises(brokerlink.LinkError):
        brokerlink.complete_link(request.token, request.signature, {"api_key": "attacker"})


def test_link_requires_a_valid_signature(hosted):
    request = brokerlink.create_link(hosted, "groww")
    with pytest.raises(brokerlink.LinkError):
        brokerlink.resolve_link(request.token, "forged-signature")


def test_expired_link_is_refused(hosted, monkeypatch):
    monkeypatch.setattr(brokerlink, "LINK_TTL_MINUTES", 0)
    request = brokerlink.create_link(hosted, "groww")
    with pytest.raises(brokerlink.LinkError, match="expired"):
        brokerlink.resolve_link(request.token, request.signature)


def test_zerodha_is_not_usable_until_its_login_completes(hosted):
    """Sealing Kite keys is not the same as having a session — reporting it as
    linked would let live activation proceed with no way to trade."""
    request = brokerlink.create_link(hosted, "zerodha")
    brokerlink.complete_link(request.token, request.signature,
                             {"api_key": "kite_key", "api_secret": "kite_secret"})
    assert brokerlink.active_link(hosted) is None

    record = brokerlink.linked_record(request.token, request.signature, "zerodha")
    brokerlink.attach_access_token(record["id"], "kite_access_token")
    link = brokerlink.active_link(hosted)
    assert link["broker"] == "zerodha"
    assert brokerlink.load_credentials(hosted, "zerodha").get("access_token") == "kite_access_token"


def test_unlink_deletes_the_credentials(hosted):
    _link_groww(hosted)
    assert brokerlink.unlink(hosted) == 1
    assert brokerlink.active_link(hosted) is None
    with pytest.raises(brokerlink.LinkError):
        brokerlink.load_credentials(hosted, "groww")


def test_one_users_link_is_invisible_to_another(hosted):
    _link_groww(hosted)
    other_user, other_account = db.upsert_user("auth0|someone-else")
    other = context_for_account(user_id=other_user, account_id=other_account,
                                trading_mode=TradingMode.PAPER, max_order_value=100_000.0)
    assert brokerlink.active_link(other) is None
    with pytest.raises(brokerlink.LinkError):
        brokerlink.load_credentials(other, "groww")


# =========================================================================== #
# Live gates — nothing reaches a real broker without passing every one
# =========================================================================== #
def test_linking_a_broker_does_not_enable_real_money(hosted):
    """The single most important property in the product."""
    _link_groww(hosted)
    assert db.load_account_row(hosted.account_id).mode == "paper"
    assert limits.account_limits(hosted)["live_activated"] is False


def test_activation_requires_a_linked_broker(hosted):
    with pytest.raises(limits.LimitExceeded, match="Link a broker"):
        limits.activate_live(hosted)


def _live_ctx(hosted):
    limits.activate_live(hosted)
    return hosted.with_mode(TradingMode.LIVE)


def test_activation_switches_the_account_live(hosted):
    _link_groww(hosted)
    live = _live_ctx(hosted)
    limits.check_live_order(live, 5_000.0)  # must not raise
    assert limits.account_limits(live)["live_activated"] is True


def test_kill_switch_blocks_the_next_order_immediately(hosted):
    _link_groww(hosted)
    live = _live_ctx(hosted)
    limits.set_kill_switch(hosted, True)
    with pytest.raises(limits.LimitExceeded, match="kill switch"):
        limits.check_live_order(live, 5_000.0)

    limits.set_kill_switch(hosted, False)
    limits.check_live_order(live, 5_000.0)


def test_operator_kill_switch_stops_everyone(hosted, monkeypatch):
    _link_groww(hosted)
    live = _live_ctx(hosted)
    monkeypatch.setenv(limits.GLOBAL_KILL_ENV, "1")
    with pytest.raises(limits.LimitExceeded, match="disabled on this server"):
        limits.check_live_order(live, 100.0)


def test_deactivating_live_blocks_orders_again(hosted):
    _link_groww(hosted)
    live = _live_ctx(hosted)
    limits.deactivate_live(hosted)
    with pytest.raises(limits.LimitExceeded, match="not activated"):
        limits.check_live_order(live, 5_000.0)


def test_unknown_order_value_fails_closed(hosted):
    """A value we cannot compute cannot be checked against a limit, so the order
    must be refused rather than waved through."""
    _link_groww(hosted)
    live = _live_ctx(hosted)
    for unknown in (None, 0, 0.0):
        with pytest.raises(limits.LimitExceeded, match="could not be determined"):
            limits.check_live_order(live, unknown)


def _record_order(ctx, value: float, status: str = "placed") -> None:
    from trinetra.broker.base import new_reference_id

    with db.session_scope() as session:
        session.add(db.Order(
            account_id=ctx.account_id, reference_id=new_reference_id(), broker="groww",
            trading_symbol="RELIANCE", exchange="NSE", transaction_type="BUY",
            quantity=1, order_type="MARKET", product="CNC",
            estimated_value=value, status=status,
        ))
        session.commit()


def test_daily_notional_cap_blocks_the_order_that_would_cross_it(hosted):
    _link_groww(hosted)
    live = _live_ctx(hosted)
    limits.set_daily_caps(hosted, notional=50_000.0)

    _record_order(live, 45_000.0)
    limits.check_live_order(live, 4_000.0)  # 49,000 — still inside
    with pytest.raises(limits.LimitExceeded, match="daily limit"):
        limits.check_live_order(live, 6_000.0)  # 51,000 — over


def test_daily_order_count_cap_is_enforced(hosted):
    _link_groww(hosted)
    live = _live_ctx(hosted)
    limits.set_daily_caps(hosted, orders=2)

    _record_order(live, 100.0)
    _record_order(live, 100.0)
    with pytest.raises(limits.LimitExceeded, match="daily limit of 2"):
        limits.check_live_order(live, 100.0)


def test_rejected_orders_do_not_consume_the_daily_budget(hosted):
    _link_groww(hosted)
    live = _live_ctx(hosted)
    limits.set_daily_caps(hosted, notional=10_000.0)
    _record_order(live, 9_000.0, status="rejected")
    limits.check_live_order(live, 5_000.0)  # the rejected one must not count


def test_paper_orders_skip_the_live_gates(hosted):
    """Paper trading has nothing to protect and must never be blocked by them."""
    limits.set_kill_switch(hosted, True)
    limits.check_live_order(hosted, None)  # paper context — must not raise


# =========================================================================== #
# Tool surface — no credential may ever cross it
# =========================================================================== #
CREDENTIAL_WORDS = ("api_key", "apikey", "secret", "password", "totp",
                    "access_token", "private_key", "passphrase")


def test_no_tool_accepts_a_credential_parameter():
    """Structural guarantee: if no tool has a credential-shaped input, no host AI
    can ever be talked into collecting one in chat."""
    import asyncio

    from trinetra_mcp.server import build_server

    offenders = []
    for tool in asyncio.run(build_server().list_tools()):
        for name in (tool.inputSchema or {}).get("properties", {}):
            lowered = name.lower()
            if any(word in lowered for word in CREDENTIAL_WORDS):
                # confirmation_token is ours, not a broker credential.
                if lowered == "confirmation_token":
                    continue
                offenders.append(f"{tool.name}.{name}")
    assert not offenders, f"tools accept credential-shaped inputs: {offenders}"


def test_link_broker_tool_returns_only_a_url(hosted, monkeypatch):
    import asyncio

    from trinetra_mcp import runtime
    from trinetra_mcp.server import build_server

    monkeypatch.setenv("PUBLIC_URL", "https://trinetra.test")
    monkeypatch.setattr(runtime, "current_context", lambda: hosted)
    import trinetra_mcp.broker_tools as bt
    monkeypatch.setattr(bt, "current_context", lambda: hosted)

    result = asyncio.run(build_server().call_tool("link_broker", {"broker": "groww"}))
    content = result[0] if isinstance(result, tuple) else result
    payload = json.loads(next(b for b in content if getattr(b, "text", None)).text)
    assert payload["status"] == "link_created"
    assert payload["url"].startswith("https://trinetra.test/link?")
    for word in ("api_key", "secret", "totp"):
        assert word not in json.dumps(payload).lower().replace("api keys", "")


# =========================================================================== #
# Switching between paper and live
# =========================================================================== #
@pytest.fixture
def live_tools(hosted, monkeypatch):
    """The tool surface bound to the hosted test account."""
    import asyncio

    from trinetra_mcp import broker_tools, runtime
    from trinetra_mcp import tools as mcp_tools
    from trinetra_mcp.server import build_server

    monkeypatch.setenv("PUBLIC_URL", "https://trinetra.test")

    def context_now():
        """Re-derive the mode per call, exactly as the hosted resolver does —
        a frozen context would never observe a mode switch."""
        row = db.load_account_row(hosted.account_id)
        mode = TradingMode(row.mode) if row else TradingMode.PAPER
        return hosted.with_mode(mode)

    for module in (runtime, broker_tools, mcp_tools):
        monkeypatch.setattr(module, "current_context", context_now, raising=False)
        monkeypatch.setattr(module, "require_account", lambda ctx: None, raising=False)
    server = build_server()

    def call(name, **kwargs):
        result = asyncio.run(server.call_tool(name, kwargs))
        content = result[0] if isinstance(result, tuple) else result
        return json.loads(next(b for b in content if getattr(b, "text", None)).text)

    return call


def test_cannot_go_live_without_a_linked_broker(live_tools):
    out = live_tools("switch_trading_mode", mode="live", confirm="I UNDERSTAND")
    assert out["status"] == "no_broker_linked"


def test_going_live_needs_the_exact_confirmation_phrase(hosted, live_tools):
    _link_groww(hosted)
    assert live_tools("switch_trading_mode", mode="live")["status"] == "confirmation_required"
    for wrong in ("yes", "i understand it", "I UNDERSTAND!", "ok go ahead", ""):
        out = live_tools("switch_trading_mode", mode="live", confirm=wrong)
        assert out["status"] == "confirmation_required", f"{wrong!r} must not pass"
    assert db.load_account_row(hosted.account_id).mode == "paper"


def test_the_phrase_is_accepted_case_and_space_insensitively(hosted, live_tools):
    _link_groww(hosted)
    out = live_tools("switch_trading_mode", mode="live", confirm="  i understand  ")
    assert out["status"] == "switched"
    assert out["is_real_money"] is True


def test_switching_back_to_paper_is_immediate(hosted, live_tools):
    _link_groww(hosted)
    live_tools("switch_trading_mode", mode="live", confirm="I UNDERSTAND")
    out = live_tools("switch_trading_mode", mode="paper")  # no confirmation needed
    assert out["status"] == "switched"
    assert out["is_real_money"] is False
    assert out["linked_broker"]["broker"] == "groww", "the link survives the switch"
    assert db.load_account_row(hosted.account_id).mode == "paper"


def test_round_trip_between_modes(hosted, live_tools):
    _link_groww(hosted)
    for _ in range(2):
        assert live_tools("switch_trading_mode", mode="live",
                          confirm="I UNDERSTAND")["mode"] == "live"
        assert live_tools("switch_trading_mode", mode="paper")["mode"] == "paper"


def test_unlinking_returns_the_account_to_paper(hosted, live_tools):
    _link_groww(hosted)
    live_tools("switch_trading_mode", mode="live", confirm="I UNDERSTAND")
    assert live_tools("unlink_broker")["status"] == "unlinked"
    assert db.load_account_row(hosted.account_id).mode == "paper", (
        "removing the credentials must not leave the account marked live"
    )


def test_relinking_replaces_the_cached_broker_session(hosted):
    """A re-link must take effect on the next call, not when a cache expires.

    Zerodha sessions are renewed every trading day, so serving the previous
    session for another five minutes would make the renewal look broken.
    """
    from trinetra.broker import registry

    _link_groww(hosted)
    # Stand in for a live adapter built from the original credentials.
    registry._cache[f"{hosted.user_id}|groww"] = ("STALE-ADAPTER", 1e18)

    request = brokerlink.create_link(hosted, "groww")
    brokerlink.complete_link(request.token, request.signature,
                             {"api_key": "rotated_key", "totp_secret": "NEWSECRET222"})

    assert f"{hosted.user_id}|groww" not in registry._cache, (
        "the adapter built from the old credentials is still cached"
    )
    assert brokerlink.load_credentials(hosted, "groww").get("api_key") == "rotated_key"


def test_kite_daily_relink_replaces_the_cached_session(hosted):
    from trinetra.broker import registry

    request = brokerlink.create_link(hosted, "zerodha")
    brokerlink.complete_link(request.token, request.signature,
                             {"api_key": "k", "api_secret": "s"})
    record = brokerlink.linked_record(request.token, request.signature, "zerodha")
    registry._cache[f"{hosted.user_id}|zerodha"] = ("YESTERDAYS-SESSION", 1e18)

    brokerlink.attach_access_token(record["id"], "todays_token")
    assert f"{hosted.user_id}|zerodha" not in registry._cache


def test_relinking_supersedes_the_previous_credentials(hosted):
    """Rotating a key must actually rotate it. Leaving the old row in place
    would keep the compromised key in use forever."""
    from sqlalchemy import select

    _link_groww(hosted)
    request = brokerlink.create_link(hosted, "groww")
    brokerlink.complete_link(request.token, request.signature,
                             {"api_key": "rotated_key", "totp_secret": "NEWSECRET222"})

    assert brokerlink.load_credentials(hosted, "groww").get("api_key") == "rotated_key"
    with db.session_scope() as session:
        rows = session.scalars(
            select(db.BrokerLink).where(db.BrokerLink.status == "linked")
        ).all()
    assert len(rows) == 1, "the superseded link should not survive"


# =========================================================================== #
# A user's broker session must never fall back to the server's own
# =========================================================================== #
def test_groww_auth_failure_never_retries_on_server_credentials(hosted, monkeypatch):
    """The worst possible bug: user A's live order executing on the operator's
    brokerage account because a 401 triggered a re-auth from server env."""
    from trinetra.broker import groww_client
    from trinetra.broker.base import BrokerError, OrderRequest
    from trinetra.broker.groww_broker import GrowwBroker

    class Expired(Exception):
        status_code = 401

    class UsersClient:
        def place_order(self, **kwargs):
            raise Expired("token expired")

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("re-authenticated from SERVER credentials")

    monkeypatch.setattr(groww_client, "get_client", must_not_be_called)
    monkeypatch.setattr(groww_client, "reset_client", must_not_be_called)

    broker = GrowwBroker(hosted, client=UsersClient())
    request = OrderRequest(trading_symbol="RELIANCE", transaction_type="buy", quantity=1)
    with pytest.raises(BrokerError, match="reconnect your Groww"):
        broker._call("place_order", foo=1)

    # And the failure must not be dressed up as a placed order.
    with pytest.raises(BrokerError):
        broker.place_order(request.normalised(), reference_price=100.0)


def test_hosted_deployment_refuses_server_wide_groww_credentials(hosted):
    """Defence in depth: the global client is unreachable when a database is
    configured, whatever calls it."""
    from trinetra.broker import groww_client
    from trinetra.broker.base import BrokerError

    with pytest.raises(BrokerError, match="multi-user deployment"):
        groww_client.get_client()
    with pytest.raises(BrokerError, match="multi-user deployment"):
        groww_client.generate_access_token()


# =========================================================================== #
# Caps must price the real exposure, not the number the caller chose
# =========================================================================== #
def test_marketable_limit_cannot_slip_under_the_cap():
    """A SELL LIMIT at ₹1 on a ₹1,400 stock fills at ₹1,400, not ₹1. Pricing it
    at the caller's number would let it evade every rupee cap."""
    from trinetra.broker.base import BrokerError, OrderRequest
    from trinetra.broker.paper_broker import PaperBroker

    request = OrderRequest(trading_symbol="RELIANCE", transaction_type="sell",
                           quantity=500, order_type="limit", price=1.0).normalised()
    # Priced against a real market of ₹1,400 the exposure is ₹7,00,000.
    assert request.estimated_value(1400.0) == 700_000.0
    with pytest.raises(BrokerError, match="safety cap"):
        PaperBroker().guard_order(request, reference_price=1400.0)


def test_far_stop_loss_trigger_is_priced_at_market():
    from trinetra.broker.base import OrderRequest

    request = OrderRequest(trading_symbol="RELIANCE", transaction_type="sell",
                           quantity=100, order_type="sl_m",
                           trigger_price=1.0).normalised()
    assert request.estimated_value(1400.0) == 140_000.0


def test_a_limit_above_market_is_still_priced_at_the_limit():
    """The worse case cuts both ways: a BUY limit above market can pay the limit."""
    from trinetra.broker.base import OrderRequest

    request = OrderRequest(trading_symbol="RELIANCE", transaction_type="buy",
                           quantity=10, order_type="limit", price=1500.0).normalised()
    assert request.estimated_value(1400.0) == 15_000.0


# =========================================================================== #
# Fixes for the remaining audited defects
# =========================================================================== #
def test_a_paper_preview_cannot_be_confirmed_after_switching_to_live(hosted, live_tools):
    """The preview's prices and consequences belong to the mode it was made in.
    Confirming a paper preview against a live account is a different trade."""
    _link_groww(hosted)
    import trinetra.market_data as market_data
    from trinetra.services import trading as trading_svc

    original = trading_svc.instruments.resolve
    trading_svc.instruments.resolve = lambda s, e=None: _instrument_stub()
    market_original = market_data.try_ltp
    market_data.try_ltp = lambda s: 100.0
    try:
        preview = live_tools("place_order", symbol="RELIANCE", action="buy", quantity=1)
        token = preview["confirmation_token"]
        live_tools("switch_trading_mode", mode="live", confirm="I UNDERSTAND")
        out = live_tools("confirm_order", confirmation_token=token)
        assert out["status"] == "rejected"
        assert "previewed in paper" in out["error"]
    finally:
        trading_svc.instruments.resolve = original
        market_data.try_ltp = market_original


def _instrument_stub():
    from trinetra.instruments import InstrumentRecord

    return InstrumentRecord(trading_symbol="RELIANCE", exchange="NSE",
                            name="Reliance Ltd", series="EQ", isin="INE002A01018",
                            lot_size=1, buy_allowed=True, sell_allowed=True)


def test_abandoned_broker_link_does_not_keep_secrets_forever(hosted, monkeypatch):
    """An unfinished Kite login leaves an api_key and secret with nothing pointing
    at them — a liability with no user benefit."""
    from sqlalchemy import select

    request = brokerlink.create_link(hosted, "zerodha")
    brokerlink.complete_link(request.token, request.signature,
                             {"api_key": "k", "api_secret": "s"})

    with db.session_scope() as session:
        rows = session.scalars(select(db.BrokerLink)).all()
        assert rows and rows[0].status == "awaiting_login", "sealed but incomplete"
        assert rows[0].secret_ciphertext, "the secrets are sitting there"
        # The user walked away and never finished the Kite login.
        rows[0].expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        session.commit()

    assert brokerlink.purge_abandoned() >= 1
    with db.session_scope() as session:
        assert not session.scalars(select(db.BrokerLink)).all()


def test_account_identity_requires_a_subject_claim(hosted, monkeypatch):
    """client_id identifies the AI host, not the person — every user of that host
    shares one, so it must never key an account."""
    from trinetra_mcp import runtime
    from trinetra_mcp.auth import TrinetraAccessToken

    token = TrinetraAccessToken(token="t", client_id="shared-mcp-client",
                                scopes=[], expires_at=None, subject="")
    monkeypatch.setattr(runtime, "get_access_token", lambda: token)
    with pytest.raises(runtime.NotAuthenticated, match="subject"):
        runtime.current_context()


def test_master_key_rotation_sorts_naturally(monkeypatch):
    """v10 must follow v9, not sort between v1 and v2."""
    keys = ",".join(f"v{n}:{MASTER_A}" for n in (1, 2, 9, 10))
    monkeypatch.setenv("TRINETRA_MASTER_KEYS", keys)
    monkeypatch.delenv("TRINETRA_ACTIVE_KEY", raising=False)
    assert vault.active_key_id() == "v10"
