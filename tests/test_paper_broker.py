"""End-to-end paper-broker behaviour: fills, persistence, holdings, funds, and
the new affordability / position guards."""
from __future__ import annotations

import pytest

from trinetra.broker.base import OrderRequest, BrokerError
from trinetra.broker.paper_broker import PaperBroker


@pytest.fixture
def broker(isolate_portfolio, monkeypatch):
    # Deterministic, offline LTP for holdings valuation.
    import trinetra.market_data as market_data

    monkeypatch.setattr(market_data, "ltp_many", lambda syms: {s: 2600.0 for s in syms})
    monkeypatch.setattr(market_data, "try_ltp", lambda s: 2600.0)
    return PaperBroker()


def _buy(broker, qty, price, symbol="RELIANCE"):
    req = OrderRequest(trading_symbol=symbol, transaction_type="BUY", quantity=qty)
    return broker.place_order(req, reference_price=price)


def _sell(broker, qty, price, symbol="RELIANCE"):
    req = OrderRequest(trading_symbol=symbol, transaction_type="SELL", quantity=qty)
    return broker.place_order(req, reference_price=price)


def test_market_buy_fills_and_persists(broker, isolate_portfolio):
    res = _buy(broker, 10, 2500.0)
    assert res.status == "filled"
    assert res.quantity == 10
    assert res.average_price == 2500.0
    assert res.estimated_value == 25000.0
    assert isolate_portfolio.exists()
    # Order shows up in history (most-recent first).
    hist = broker.get_order_history()
    assert hist[0]["symbol"] == "RELIANCE"
    assert hist[0]["action"] == "buy"


def test_market_order_without_reference_price_rejected(broker):
    req = OrderRequest(trading_symbol="RELIANCE", transaction_type="BUY", quantity=1)
    with pytest.raises(BrokerError, match="reference price"):
        broker.place_order(req, reference_price=None)


def test_holdings_and_pnl(broker):
    _buy(broker, 10, 2500.0)
    holdings = broker.get_holdings()
    assert len(holdings) == 1
    h = holdings[0]
    assert h.trading_symbol == "RELIANCE"
    assert h.quantity == 10
    assert h.average_price == 2500.0
    assert h.last_price == 2600.0
    assert h.invested == 25000.0
    assert h.current_value == 26000.0
    assert h.pnl == 1000.0


def test_cost_basis_correct_after_buy_sell_buy(broker):
    _buy(broker, 10, 100.0)   # 10 @ 100
    _sell(broker, 5, 120.0)   # sell 5 -> 5 left, avg should stay 100
    _buy(broker, 5, 200.0)    # +5 @ 200 -> 10 total, avg = (5*100 + 5*200)/10 = 150
    h = next(h for h in broker.get_holdings() if h.trading_symbol == "RELIANCE")
    assert h.quantity == 10
    assert h.average_price == 150.0


def test_cannot_oversell(broker):
    _buy(broker, 5, 100.0)
    with pytest.raises(BrokerError, match="holds only"):
        _sell(broker, 10, 100.0)


def test_insufficient_cash_rejected(broker, set_cap):
    set_cap(10_000_000.0)  # lift the per-order cap so cash is the binding limit
    # starting cash defaults to 100,000; try to spend far more.
    with pytest.raises(BrokerError, match="Insufficient paper cash"):
        _buy(broker, 1000, 2000.0)


def test_funds_reflect_spend(broker):
    start = broker.get_funds().available_cash
    _buy(broker, 10, 2500.0)  # spend 25,000
    after = broker.get_funds()
    assert after.available_cash == round(start - 25000.0, 2)
    # net worth = cash + market value of holdings (10 * 2600 = 26,000)
    assert after.net == round(after.available_cash + 26000.0, 2)


def test_sl_orders_rejected_in_paper(broker):
    req = OrderRequest(trading_symbol="RELIANCE", transaction_type="BUY", quantity=1,
                       order_type="SL", price=100, trigger_price=99)
    with pytest.raises(BrokerError, match="aren't simulated|Stop-loss"):
        broker.place_order(req, reference_price=100.0)


# --- realized P&L + performance ---------------------------------------------
def test_holdings_carry_realised_pnl(broker):
    _buy(broker, 10, 1290.0, symbol="ICICIBANK")
    _sell(broker, 3, 1380.0, symbol="ICICIBANK")
    h = next(h for h in broker.get_holdings() if h.trading_symbol == "ICICIBANK")
    assert h.quantity == 7
    assert h.realised_pnl == 270.0          # (1380-1290)*3
    assert h.average_price == 1290.0        # basis unchanged by the sell
    assert h.holding_days is not None and h.holding_days >= 0


def test_realized_total_includes_closed_positions(broker, monkeypatch):
    # ITC fully sold at a loss -> not in holdings, but must count in realized total.
    _buy(broker, 35, 283.33, symbol="ITC")
    _sell(broker, 35, 270.0, symbol="ITC")
    _buy(broker, 10, 1290.0, symbol="ICICIBANK")
    _sell(broker, 3, 1380.0, symbol="ICICIBANK")
    total = broker.realized_total()
    assert total == round((270.0 - 283.33) * 35 + (1380.0 - 1290.0) * 3, 2)


def test_performance_payload(broker, monkeypatch):
    import trinetra.market_data as market_data
    monkeypatch.setattr(market_data, "get_live_quote", lambda s: {"day_change": 0.0})
    _buy(broker, 10, 100.0, symbol="AAA")
    _sell(broker, 10, 130.0, symbol="AAA")   # +300, closed
    _buy(broker, 10, 100.0, symbol="BBB")
    _sell(broker, 5, 80.0, symbol="BBB")     # -100, still holding 5
    perf = broker.performance()
    assert perf["available"] is True
    assert perf["total_realized"] == 200.0
    assert perf["stats"]["winners"] == 1
    assert perf["stats"]["losers"] == 1
    assert perf["best_trade"]["symbol"] == "AAA"
    assert perf["worst_trade"]["symbol"] == "BBB"
    assert any(c["symbol"] == "AAA" for c in perf["closed_positions"])
