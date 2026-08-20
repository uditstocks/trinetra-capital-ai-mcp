"""End-to-end through the agent-facing @tool functions (the real surface the
LLMs invoke), in both paper and live mode, with broker/market-data stubbed."""
from __future__ import annotations

import json

import pytest

from trinetra import tools
from trinetra.instruments import InstrumentRecord
from trinetra.services import trading as trading_svc


def _rec(symbol="RELIANCE", exchange="NSE", buy=True, sell=True):
    return InstrumentRecord(
        trading_symbol=symbol, exchange=exchange, name=f"{symbol} Ltd",
        series="EQ", isin="INE000000001", lot_size=1,
        buy_allowed=buy, sell_allowed=sell,
    )


@pytest.fixture
def offline(isolate_portfolio, monkeypatch):
    import trinetra.market_data as market_data
    # Authoritative resolution without the instrument-master network.
    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _rec())
    monkeypatch.setattr(trading_svc.instruments, "search", lambda s, limit=3: [])
    monkeypatch.setattr(market_data, "try_ltp", lambda s: 2500.0)
    monkeypatch.setattr(market_data, "ltp_many", lambda syms: {s: 2600.0 for s in syms})
    return market_data


def test_place_order_paper_e2e(offline):
    out = json.loads(tools.place_order.invoke({
        "symbol": "RELIANCE", "action": "buy", "quantity": 10, "order_type": "market"}))
    assert out["status"] == "filled"
    assert out["trading_mode"] == "paper"
    assert out["estimated_value"] == 25000.0


def test_place_order_rejects_unknown_symbol(isolate_portfolio, monkeypatch):
    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: None)
    monkeypatch.setattr(trading_svc.instruments, "search", lambda s, limit=3: [])
    out = json.loads(tools.place_order.invoke({
        "symbol": "NOTAREALSTOCK", "action": "buy", "quantity": 1}))
    assert out["status"] == "rejected"
    assert "Could not find" in out["error"]


def test_place_order_blocks_over_cap(offline, set_cap):
    set_cap(100_000.0)
    out = json.loads(tools.place_order.invoke({
        "symbol": "RELIANCE", "action": "buy", "quantity": 1000, "order_type": "market"}))
    # 1000 * 2500 = 2.5M > cap -> rejected by guard, surfaced as a clean error.
    assert out["status"] in ("rejected", "failed")
    assert "safety cap" in out["error"]


def test_view_portfolio_paper_has_display(offline):
    tools.place_order.invoke({"symbol": "RELIANCE", "action": "buy",
                              "quantity": 10, "order_type": "market"})
    out = json.loads(tools.view_portfolio.invoke({}))
    assert out["mode"] == "paper"
    assert "display" in out
    assert "RELIANCE" in out["display"]
    assert out["summary"]["holdings_count"] == 1


def test_get_funds_paper(offline):
    out = json.loads(tools.get_funds.invoke({}))
    assert out["mode"] == "paper"
    assert out["available_cash"] > 0


def test_buy_disabled_symbol_rejected(isolate_portfolio, monkeypatch):
    import trinetra.market_data as market_data
    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _rec(buy=False))
    monkeypatch.setattr(market_data, "try_ltp", lambda s: 2500.0)
    out = json.loads(tools.place_order.invoke({
        "symbol": "RELIANCE", "action": "buy", "quantity": 1}))
    assert out["status"] == "rejected"
    assert "not buy-enabled" in out["error"]
