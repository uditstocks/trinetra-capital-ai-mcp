"""Realized (booked) P&L analytics — the maths that answers 'am I net positive?'."""
from __future__ import annotations

from datetime import date

import pytest

from trinetra import analytics


def _t(symbol, action, shares, price, ts="2026-06-01T10:00:00"):
    return {"symbol": symbol, "action": action, "shares": shares, "price": price,
            "total": round(shares * price, 2), "timestamp": ts}


def test_realized_pnl_basic_profit():
    # The user's ICICI case: buy 10 @ 1290, sell 3 @ 1380 -> +270 booked.
    a = analytics.analyze([
        _t("ICICIBANK", "buy", 10, 1290),
        _t("ICICIBANK", "sell", 3, 1380),
    ])
    assert a.total_realized == 270.0
    assert a.realized_by_symbol() == {"ICICIBANK": 270.0}
    # 7 shares still held at unchanged avg cost.
    assert a.per_symbol["ICICIBANK"].qty == 7
    assert round(a.per_symbol["ICICIBANK"].avg_cost, 2) == 1290.0


def test_realized_pnl_loss_and_full_exit():
    a = analytics.analyze([
        _t("ITC", "buy", 35, 283.33),
        _t("ITC", "sell", 35, 270.0),
    ])
    assert a.total_realized == round((270.0 - 283.33) * 35, 2)
    assert a.per_symbol["ITC"].qty == 0
    closed = a.closed_positions()
    assert len(closed) == 1
    assert closed[0]["symbol"] == "ITC"
    assert closed[0]["realized"] == a.total_realized


def test_avg_cost_method_across_buy_sell_buy():
    # buy 10@100, sell 5@120 (+100), buy 5@200 -> avg of remaining 10 = 150.
    a = analytics.analyze([
        _t("X", "buy", 10, 100),
        _t("X", "sell", 5, 120),
        _t("X", "buy", 5, 200),
    ])
    assert a.total_realized == 100.0   # (120-100)*5
    st = a.per_symbol["X"]
    assert st.qty == 10
    assert round(st.avg_cost, 2) == 150.0


def test_stats_win_rate_and_gross():
    a = analytics.analyze([
        _t("A", "buy", 10, 100), _t("A", "sell", 10, 130),   # +300 win
        _t("B", "buy", 10, 100), _t("B", "sell", 10, 80),    # -200 loss
        _t("C", "buy", 10, 100), _t("C", "sell", 10, 110),   # +100 win
    ])
    s = a.stats()
    assert s["closed_trades"] == 3
    assert s["winners"] == 2
    assert s["losers"] == 1
    assert s["win_rate"] == round(2 / 3 * 100, 1)
    assert s["gross_profit"] == 400.0
    assert s["gross_loss"] == -200.0
    assert s["net_realized"] == 200.0
    assert a.total_realized == 200.0


def test_best_and_worst_trade():
    a = analytics.analyze([
        _t("A", "buy", 10, 100), _t("A", "sell", 10, 130),   # +300
        _t("B", "buy", 10, 100), _t("B", "sell", 10, 50),    # -500
    ])
    assert a.best_trade().symbol == "A"
    assert a.best_trade().realized == 300.0
    assert a.worst_trade().symbol == "B"
    assert a.worst_trade().realized == -500.0


def test_realized_today_only_counts_today():
    a = analytics.analyze([
        _t("A", "buy", 10, 100, ts="2026-06-01T10:00:00"),
        _t("A", "sell", 5, 120, ts="2026-06-01T11:00:00"),   # old
        _t("A", "sell", 5, 130, ts="2026-07-10T09:00:00"),   # "today"
    ])
    today = date(2026, 7, 10)
    assert a.realized_today(today) == round((130 - 100) * 5, 2)
    assert a.realized_today(None) == 0.0


def test_no_sells_means_no_realized():
    a = analytics.analyze([_t("A", "buy", 10, 100)])
    assert a.total_realized == 0.0
    assert a.stats()["closed_trades"] == 0
    assert a.best_trade() is None
    assert a.closed_positions() == []


def test_symbols_normalised_together():
    a = analytics.analyze([
        _t("RELIANCE.NS", "buy", 10, 100),
        _t("NSE_RELIANCE", "sell", 10, 120),
    ])
    assert list(a.per_symbol) == ["RELIANCE"]
    assert a.total_realized == 200.0


def test_zero_or_missing_shares_ignored():
    a = analytics.analyze([
        {"symbol": "A", "action": "buy", "shares": 0, "price": 100},
        {"action": "buy", "shares": 5, "price": 100},  # no symbol
        _t("A", "buy", 10, 100),
    ])
    assert a.per_symbol["A"].qty == 10
