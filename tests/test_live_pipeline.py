"""The live-broker pipeline, exercised end to end against fake broker SDKs.

Every other test either uses paper mode or mocks a broker object directly, which
means the wiring between them was never executed: vault -> registry ->
build_client -> adapter -> SDK. That gap is exactly how the deployed server
shipped without `growwapi` installed and nobody noticed until a real order.

These fakes stand in for the broker SDKs at the module level, so the real
registry, the real vault, the real adapters and the real order path all run. The
only thing that is not real is the HTTP call to the exchange.
"""
from __future__ import annotations

import base64
import sys
import types
from typing import ClassVar

import pytest

from trinetra import brokerlink, db, limits
from trinetra.config import TradingMode
from trinetra.session import context_for_account

MASTER = base64.b64encode(b"L" * 32).decode()
GROWW_CREDS = {"api_key": "gw_key_abc", "totp_secret": "JBSWY3DPEHPK3PXP"}
KITE_CREDS = {"api_key": "kite_key", "api_secret": "kite_secret"}


# --------------------------------------------------------------------------- #
# fake SDKs
# --------------------------------------------------------------------------- #
class FakeGrowwAPI:
    """Stands in for growwapi.GrowwAPI. Records what it was asked to do."""

    calls: ClassVar[list[tuple[str, dict]]] = []
    built_with: ClassVar[list[str]] = []
    auth_args: ClassVar[list[dict]] = []

    # The SDK exposes its vocabulary as class constants; the adapter reads them
    # defensively via getattr, so the names have to match the real ones.
    EXCHANGE_NSE = "NSE"
    EXCHANGE_BSE = "BSE"
    SEGMENT_CASH = "CASH"
    PRODUCT_CNC = "CNC"
    PRODUCT_MIS = "MIS"
    ORDER_TYPE_MARKET = "MARKET"
    ORDER_TYPE_LIMIT = "LIMIT"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"
    VALIDITY_DAY = "DAY"

    def __init__(self, token):
        FakeGrowwAPI.built_with.append(token)

    @classmethod
    def get_access_token(cls, **kwargs):
        cls.auth_args.append(kwargs)
        return f"token-for-{kwargs.get('api_key')}"

    def place_order(self, **params):
        FakeGrowwAPI.calls.append(("place_order", params))
        return {"groww_order_id": "GW-1", "order_status": "placed"}

    holdings_payload: ClassVar[dict] = {"holdings": []}

    def get_holdings_for_user(self, **kwargs):
        FakeGrowwAPI.calls.append(("get_holdings_for_user", kwargs))
        return FakeGrowwAPI.holdings_payload

    def get_positions_for_user(self, **kwargs):
        FakeGrowwAPI.calls.append(("get_positions_for_user", kwargs))
        return {"positions": [
            {"trading_symbol": "TCS", "quantity": 5, "product": "MIS",
             "segment": "CASH", "net_price": 3900.0, "realised_pnl": 120.0},
        ]}

    def get_available_margin_details(self, **kwargs):
        FakeGrowwAPI.calls.append(("get_available_margin_details", kwargs))
        return {"clear_cash": 50000.0, "net_margin_used": 1200.0,
                "equity_margin_details": {"cnc_balance_available": 50000.0}}

    def get_ltp(self, **kwargs):
        # Keyed by "NSE_SYMBOL" tokens, exactly as the real SDK returns them.
        return dict.fromkeys(kwargs.get("exchange_trading_symbols", ()), 1600.0)

    def get_order_list(self, **kwargs):
        FakeGrowwAPI.calls.append(("get_order_list", kwargs))
        return {"order_list": [
            {"groww_order_id": "GW-1", "trading_symbol": "INFY",
             "transaction_type": "BUY", "quantity": 2, "order_status": "FILLED",
             "average_price": 1600.0, "order_type": "MARKET", "product": "CNC"},
        ]}

    def get_order_status(self, **kwargs):
        return {"order_status": "FILLED", "quantity": 2, "average_price": 1600.0}

    def cancel_order(self, **kwargs):
        FakeGrowwAPI.calls.append(("cancel_order", kwargs))
        return {"order_status": "CANCELLED"}


class FakeKite:
    calls: ClassVar[list[tuple[str, dict]]] = []
    built_with: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, api_key=None, **kwargs):
        self.api_key = api_key

    def set_access_token(self, token):
        FakeKite.built_with.append((self.api_key, token))

    def place_order(self, **params):
        FakeKite.calls.append(("place_order", params))
        return "KITE-1"

    def ltp(self, instruments=None):
        return {instruments[0]: {"last_price": 1500.0}}

    def holdings(self):
        return []

    def positions(self):
        return {"net": []}

    def margins(self, segment=None):
        return {"available": {"live_balance": 50000.0}, "utilised": {"debits": 0.0}}


@pytest.fixture
def fake_sdks(monkeypatch):
    """Install fake broker SDKs so the real wiring runs against them."""
    FakeGrowwAPI.calls.clear()
    FakeGrowwAPI.built_with.clear()
    FakeGrowwAPI.auth_args.clear()
    FakeGrowwAPI.holdings_payload = {"holdings": [
        {"trading_symbol": "INFY", "quantity": 10, "average_price": 1500.0},
        {"trading_symbol": "RELIANCE", "quantity": 4, "average_price": 1300.0},
    ]}
    FakeKite.calls.clear()
    FakeKite.built_with.clear()

    groww = types.ModuleType("growwapi")
    groww.GrowwAPI = FakeGrowwAPI
    monkeypatch.setitem(sys.modules, "growwapi", groww)

    kite = types.ModuleType("kiteconnect")
    kite.KiteConnect = FakeKite
    monkeypatch.setitem(sys.modules, "kiteconnect", kite)

    otp = types.ModuleType("pyotp")
    otp.TOTP = lambda secret: types.SimpleNamespace(now=lambda: "123456")
    monkeypatch.setitem(sys.modules, "pyotp", otp)


@pytest.fixture
def live(tmp_path, monkeypatch, fake_sdks):
    """A hosted account with a linked broker and live trading switched on."""
    import trinetra.market_data as market_data
    from trinetra.broker import registry

    monkeypatch.setenv("TRINETRA_MASTER_KEY", MASTER)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'p.db').as_posix()}")
    monkeypatch.delenv(limits.GLOBAL_KILL_ENV, raising=False)
    monkeypatch.setattr(market_data, "try_ltp", lambda s: 1500.0)
    monkeypatch.setattr(market_data, "ltp_many", lambda syms: dict.fromkeys(syms, 1500.0))

    db.reset_engine()
    db.Base.metadata.create_all(db.get_engine())
    registry.clear()
    user_id, account_id = db.upsert_user("auth0|live-trader")
    ctx = context_for_account(user_id=user_id, account_id=account_id,
                              trading_mode=TradingMode.PAPER, max_order_value=100_000.0)
    yield ctx
    registry.clear()
    db.reset_engine()


def _link(ctx, broker: str, creds: dict) -> None:
    request = brokerlink.create_link(ctx, broker)
    brokerlink.complete_link(request.token, request.signature, dict(creds))
    if broker == "zerodha":
        record = brokerlink.linked_record(request.token, request.signature, "zerodha")
        brokerlink.attach_access_token(record["id"], "kite-session-token")


def _go_live(ctx):
    limits.activate_live(ctx)
    return ctx.with_mode(TradingMode.LIVE)


def _instrument(symbol="INFY", exchange="NSE"):
    from trinetra.instruments import InstrumentRecord

    return InstrumentRecord(trading_symbol=symbol, exchange=exchange,
                            name=f"{symbol} Ltd", series="EQ", isin="INE009A01021",
                            lot_size=1, buy_allowed=True, sell_allowed=True)


# =========================================================================== #
# Groww
# =========================================================================== #
def test_groww_live_order_reaches_the_sdk_with_the_right_instruction(live, monkeypatch):
    """The whole chain: vault -> registry -> build_client -> adapter -> SDK."""
    from trinetra.services import trading as trading_svc

    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _instrument())
    _link(live, "groww", GROWW_CREDS)
    ctx = _go_live(live)

    preview = trading_svc.preview_order(ctx, "infosys", "buy", 2)
    assert preview["status"] == "preview", preview.get("error")

    result = trading_svc.execute_order(ctx, preview["order"])
    assert result["status"] == "placed", result.get("error")

    assert FakeGrowwAPI.calls, "nothing ever reached the broker SDK"
    _, params = FakeGrowwAPI.calls[-1]
    assert params["trading_symbol"] == "INFY"
    assert params["quantity"] == 2
    assert params["transaction_type"] == "BUY"
    assert params["exchange"] == "NSE"
    assert params["product"] == "CNC"


def test_groww_authenticates_with_the_users_own_vaulted_key(live, monkeypatch):
    from trinetra.services import trading as trading_svc

    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _instrument())
    _link(live, "groww", GROWW_CREDS)
    ctx = _go_live(live)
    trading_svc.preview_order(ctx, "infosys", "buy", 1)

    assert FakeGrowwAPI.auth_args, "never authenticated"
    assert FakeGrowwAPI.auth_args[-1]["api_key"] == GROWW_CREDS["api_key"], (
        "authenticated with something other than this user's stored key"
    )


def test_live_order_is_recorded_and_counted_against_the_daily_cap(live, monkeypatch):
    from sqlalchemy import select

    from trinetra.services import trading as trading_svc

    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _instrument())
    _link(live, "groww", GROWW_CREDS)
    ctx = _go_live(live)

    preview = trading_svc.preview_order(ctx, "infosys", "buy", 2)
    trading_svc.execute_order(ctx, preview["order"])

    with db.session_scope() as session:
        orders = session.scalars(select(db.Order)).all()
        events = session.scalars(select(db.AuditEvent)).all()
    assert len(orders) == 1
    assert orders[0].trading_symbol == "INFY"
    assert orders[0].broker_order_id == "GW-1"
    assert {e.event for e in events} >= {"preview", "submit", "filled"}
    assert limits.daily_usage(ctx).orders == 1


def test_kill_switch_stops_a_live_order_before_the_sdk(live, monkeypatch):
    from trinetra.services import trading as trading_svc

    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _instrument())
    _link(live, "groww", GROWW_CREDS)
    ctx = _go_live(live)
    preview = trading_svc.preview_order(ctx, "infosys", "buy", 1)

    limits.set_kill_switch(live, True)
    FakeGrowwAPI.calls.clear()
    result = trading_svc.execute_order(ctx, preview["order"])

    assert result["status"] == "rejected"
    assert not FakeGrowwAPI.calls, "the order reached the broker despite the kill switch"


def test_per_order_cap_blocks_before_the_sdk(live, monkeypatch):
    from trinetra.services import trading as trading_svc

    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _instrument())
    _link(live, "groww", GROWW_CREDS)
    ctx = _go_live(live)

    # 1000 x 1500 = 15,00,000, well past the 1,00,000 cap.
    out = trading_svc.preview_order(ctx, "infosys", "buy", 1000)
    assert out["status"] == "rejected"
    assert "safety cap" in out["error"]
    assert not FakeGrowwAPI.calls


# =========================================================================== #
# Zerodha
# =========================================================================== #
def test_zerodha_live_order_reaches_kite_with_the_right_instruction(live, monkeypatch):
    from trinetra.services import trading as trading_svc

    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _instrument())
    _link(live, "zerodha", KITE_CREDS)
    ctx = _go_live(live)

    preview = trading_svc.preview_order(ctx, "infosys", "buy", 3)
    assert preview["status"] == "preview", preview.get("error")
    result = trading_svc.execute_order(ctx, preview["order"])
    assert result["status"] == "placed", result.get("error")

    assert FakeKite.calls, "nothing reached the Kite SDK"
    _, params = FakeKite.calls[-1]
    assert params["tradingsymbol"] == "INFY"
    assert params["quantity"] == 3
    assert params["transaction_type"] == "BUY"
    assert params["exchange"] == "NSE"
    assert params["product"] == "CNC"
    assert params["order_type"] == "MARKET"
    assert FakeKite.built_with[-1] == ("kite_key", "kite-session-token")


def test_zerodha_without_a_session_refuses_rather_than_guessing(live, monkeypatch):
    """Sealing Kite keys is not a session. Trading before the redirect login
    completes must fail clearly, not silently."""
    from trinetra.services import trading as trading_svc

    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _instrument())
    request = brokerlink.create_link(live, "zerodha")
    brokerlink.complete_link(request.token, request.signature, dict(KITE_CREDS))

    with pytest.raises(limits.LimitExceeded, match="Link a broker"):
        limits.activate_live(live)


# =========================================================================== #
# the failure that actually happened in production
# =========================================================================== #
def test_a_missing_broker_sdk_is_reported_clearly(live, monkeypatch):
    """The deployed server shipped without growwapi. The user saw a raw
    ModuleNotFoundError; they should get an actionable message instead."""
    from trinetra.services import trading as trading_svc

    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _instrument())
    _link(live, "groww", GROWW_CREDS)
    ctx = _go_live(live)
    monkeypatch.delitem(sys.modules, "growwapi")
    monkeypatch.setattr(
        "builtins.__import__",
        _blocking_import("growwapi", __import__),
    )

    out = trading_svc.preview_order(ctx, "infosys", "buy", 1)
    assert out["status"] == "rejected"
    assert "growwapi" in out["error"] or "not installed" in out["error"].lower()


def _blocking_import(blocked: str, real):
    def _import(name, *args, **kwargs):
        if name == blocked:
            raise ImportError(f"No module named {blocked!r}")
        return real(name, *args, **kwargs)

    return _import


# =========================================================================== #
# gaps found while auditing the live path
# =========================================================================== #
def test_live_sell_beyond_holdings_warns_but_does_not_block(live, monkeypatch):
    """The user must be told before approving — but we must not refuse it.

    Our holdings view is a normalisation across brokers and does not model T+1
    settlement or pledged shares. Refusing on it would block valid sells; the
    broker is the authority.
    """
    from trinetra.services import trading as trading_svc

    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _instrument())
    _link(live, "groww", GROWW_CREDS)
    ctx = _go_live(live)

    out = trading_svc.preview_order(ctx, "infosys", "sell", 50)
    assert out["status"] == "preview", "a live sell must not be refused on our own estimate"
    warnings = " ".join(out["preview"]["warnings"]).lower()
    assert "holdings" in warnings and "50" in warnings
    assert "broker will decide" in warnings


def test_live_buy_beyond_reported_cash_warns_but_does_not_block(live, monkeypatch):
    """Groww can report clear_cash 0 on an account that can still buy. Blocking
    on that number would stop a perfectly good order."""
    from trinetra.services import trading as trading_svc

    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _instrument())
    _link(live, "groww", GROWW_CREDS)
    ctx = _go_live(live)

    out = trading_svc.preview_order(ctx, "infosys", "buy", 40)  # 60,000 vs 50,000
    assert out["status"] == "preview"
    warnings = " ".join(out["preview"]["warnings"]).lower()
    assert "available" in warnings and "broker will decide" in warnings

    # And it genuinely still reaches the broker when confirmed.
    result = trading_svc.execute_order(ctx, out["order"])
    assert result["status"] == "placed"
    assert FakeGrowwAPI.calls


def test_a_failed_balance_lookup_produces_no_warning(live, monkeypatch):
    """A timed-out funds call tells us nothing. It must not surface as a warning
    that reads like something is wrong with the order."""
    from trinetra.broker.base import BrokerError
    from trinetra.services import trading as trading_svc

    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _instrument())
    _link(live, "groww", GROWW_CREDS)
    ctx = _go_live(live)

    def boom(**kwargs):
        raise BrokerError("margin service unavailable")

    monkeypatch.setattr(FakeGrowwAPI, "get_available_margin_details", boom)
    out = trading_svc.preview_order(ctx, "infosys", "buy", 40)
    assert out["status"] == "preview"
    assert not [w for w in out["preview"]["warnings"] if "available" in w.lower()]


# =========================================================================== #
# every read a live user performs
# =========================================================================== #
@pytest.fixture
def groww_live(live, monkeypatch):
    """Live account with Groww linked, ready to read from."""
    from trinetra.services import trading as trading_svc

    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _instrument())
    _link(live, "groww", GROWW_CREDS)
    return _go_live(live)


def test_live_portfolio_shows_the_real_broker_holdings(groww_live):
    """The mapping from Groww's payload to our dataclasses — where a renamed
    field silently produces an empty portfolio."""
    from trinetra.services import trading as trading_svc

    out = trading_svc.view_portfolio(groww_live)
    assert not out.get("error"), out.get("error")
    assert out["mode"] == "live"

    by_symbol = {h["trading_symbol"]: h for h in out["holdings"]}
    assert set(by_symbol) == {"INFY", "RELIANCE"}, "broker holdings did not map through"

    infy = by_symbol["INFY"]
    assert infy["quantity"] == 10
    assert infy["average_price"] == 1500.0
    assert infy["last_price"] == 1600.0, "live LTP not applied"
    assert infy["invested"] == 15000.0
    assert infy["current_value"] == 16000.0
    assert infy["pnl"] == 1000.0
    assert infy["pnl_pct"] == pytest.approx(6.67, abs=0.01)

    summary = out["summary"]
    assert summary["holdings_count"] == 2
    assert summary["current_value"] == 16000.0 + 6400.0
    assert out["display"], "no rendered table for the user"


def test_live_balance_is_the_real_broker_balance(groww_live):
    from trinetra.services import trading as trading_svc

    funds = trading_svc.get_funds(groww_live)
    assert not funds.get("error")
    assert funds["available_cash"] == 50000.0
    assert funds["margin_used"] == 1200.0
    assert funds["mode"] == "live"


def test_live_positions_come_through(groww_live):
    from trinetra.services import trading as trading_svc

    out = trading_svc.view_portfolio(groww_live)
    positions = {p["trading_symbol"]: p for p in out["positions"]}
    assert "TCS" in positions, "intraday positions missing"
    assert positions["TCS"]["quantity"] == 5
    assert positions["TCS"]["product"] == "MIS"


def test_live_order_history_comes_from_the_broker(groww_live):
    from trinetra.services import trading as trading_svc

    out = trading_svc.get_order_history(groww_live, limit=10)
    assert not out.get("error")
    assert out["mode"] == "live"
    assert out["count"] == 1
    assert out["orders"][0]["trading_symbol"] == "INFY"
    assert out["display"], "no rendered order table"


def test_live_performance_is_honest_about_what_it_cannot_know(groww_live):
    """Neither broker exposes historical booked P&L for delivery holdings.
    Saying so is correct; inventing a number would not be."""
    from trinetra.services import trading as trading_svc

    out = trading_svc.get_performance(groww_live)
    assert out.get("available") is False
    assert "paper" in out.get("error", "").lower()
    assert out["display"], "the user still needs something readable"


def test_live_order_status_and_cancel_reach_the_broker(groww_live):
    from trinetra.broker import get_broker

    broker = get_broker(groww_live)
    status = broker.get_order_status("GW-1")
    assert status["status"] == "filled"

    FakeGrowwAPI.calls.clear()
    result = broker.cancel_order("GW-1")
    assert result["status"].lower().startswith("cancel")
    assert any(name == "cancel_order" for name, _ in FakeGrowwAPI.calls)


def test_switching_back_to_paper_shows_the_paper_book_not_the_real_one(live, monkeypatch):
    """The two modes keep separate books. A user who switches to paper must not
    see their real holdings, and vice versa."""
    from trinetra.services import trading as trading_svc

    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _instrument())
    _link(live, "groww", GROWW_CREDS)
    ctx_live = _go_live(live)
    assert len(trading_svc.view_portfolio(ctx_live)["holdings"]) == 2

    limits.deactivate_live(live)
    paper = trading_svc.view_portfolio(live)
    assert paper["mode"] == "paper"
    assert paper["holdings"] == [], "paper mode leaked the real broker holdings"


# --------------------------------------------------------------------------- #
# the same reads through the MCP tool surface a host actually calls
# --------------------------------------------------------------------------- #
def test_every_live_tool_answers_without_error(groww_live, monkeypatch):
    import asyncio
    import json

    from trinetra_mcp import broker_tools, runtime
    from trinetra_mcp import tools as mcp_tools
    from trinetra_mcp.server import build_server

    monkeypatch.setenv("PUBLIC_URL", "https://trinetra.test")
    for module in (runtime, broker_tools, mcp_tools):
        monkeypatch.setattr(module, "current_context", lambda: groww_live, raising=False)
        monkeypatch.setattr(module, "require_account", lambda ctx: None, raising=False)
    server = build_server()

    def call(name, **kwargs):
        result = asyncio.run(server.call_tool(name, kwargs))
        content = result[0] if isinstance(result, tuple) else result
        return json.loads(next(b for b in content if getattr(b, "text", None)).text)

    status = call("get_account_status")
    assert status["mode"] == "live" and status["is_real_money"] is True

    assert call("view_portfolio")["summary"]["holdings_count"] == 2
    assert call("get_funds")["available_cash"] == 50000.0
    assert call("get_order_history")["count"] == 1
    assert call("get_performance").get("available") is False

    broker_status = call("get_broker_status")
    assert broker_status["linked_broker"]["broker"] == "groww"
    assert broker_status["live_activated"] is True
    assert broker_status["kill_switch"] is False

    order = call("place_order", symbol="infosys", action="buy", quantity=1)
    assert order["status"] == "confirmation_required"
    assert order["preview"]["is_real_money"] is True
    filled = call("confirm_order", confirmation_token=order["confirmation_token"])
    assert filled["status"] == "placed"
