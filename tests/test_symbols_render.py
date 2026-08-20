"""Pure units: symbol normalisation and deterministic renderers."""
from __future__ import annotations

from trinetra import render
from trinetra.symbols import normalize


def test_normalize_variants():
    assert normalize("reliance.ns").trading_symbol == "RELIANCE"
    assert normalize("reliance.ns").exchange == "NSE"
    assert normalize("TCS.BO").exchange == "BSE"
    assert normalize("NSE_INFY").trading_symbol == "INFY"
    assert normalize("NSE_INFY").exchange == "NSE"
    assert normalize("WIPRO", "BSE").exchange == "BSE"


def test_normalize_strips_exchange_prefix():
    """Regression: 'NSE:RELIANCE' must not become the invalid yf symbol
    'NSE:RELIANCE.NS' (which 404'd every quote in the field)."""
    assert normalize("NSE:RELIANCE").trading_symbol == "RELIANCE"
    assert normalize("NSE:RELIANCE").yf_symbol == "RELIANCE.NS"
    assert normalize("nse:sbin.ns").yf_symbol == "SBIN.NS"
    assert normalize("BSE:TCS").exchange == "BSE"
    assert normalize("NSE-ONGC").trading_symbol == "ONGC"
    assert normalize("NSE TATAMOTORS").trading_symbol == "TATAMOTORS"
    # colon prefix must never survive into the tradable symbol
    assert ":" not in normalize("NSE:RELIANCE").trading_symbol


def test_exchange_token_and_yf():
    inst = normalize("RELIANCE")
    assert inst.exchange_token == "NSE_RELIANCE"
    assert inst.yf_symbol == "RELIANCE.NS"


def test_render_portfolio_empty():
    out = render.render_portfolio({"mode": "paper", "holdings": [], "funds": {"available_cash": 1000}})
    assert "No holdings" in out
    assert "1,000.00" in out


def test_render_portfolio_table():
    data = {
        "mode": "live",
        "holdings": [{"trading_symbol": "RELIANCE", "quantity": 10, "average_price": 2400,
                      "last_price": 2600, "invested": 24000, "current_value": 26000,
                      "pnl": 2000, "pnl_pct": 8.33}],
        "summary": {"total_invested": 24000, "current_value": 26000, "total_pnl": 2000,
                    "holdings_count": 1},
    }
    out = render.render_portfolio(data)
    assert "RELIANCE" in out
    assert "LIVE" in out
    assert "+₹2,000.00" in out


def test_render_orders():
    orders = [{"symbol": "RELIANCE", "action": "buy", "shares": 10, "price": 2500,
               "order_type": "MARKET", "status": "filled", "timestamp": "2026-06-30T10:00:00.123"}]
    out = render.render_orders(orders)
    assert "RELIANCE" in out
    assert "BUY" in out
    assert "2026-06-30 10:00:00" in out


def test_render_orders_empty():
    assert "No orders" in render.render_orders([])
