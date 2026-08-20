"""Charts: rendered correctly, and never at the cost of the numbers.

A chart is a bonus on top of the data. These check it actually arrives, and that
a rendering failure degrades to the table rather than losing the user's
portfolio.
"""
from __future__ import annotations

import asyncio
import json
import struct

import pytest

from trinetra import charts

PORTFOLIO = {
    "holdings": [
        {"trading_symbol": "RELIANCE", "quantity": 9, "current_value": 11799.0, "pnl": -261.0},
        {"trading_symbol": "INFY", "quantity": 10, "current_value": 11198.0, "pnl": 684.0},
        {"trading_symbol": "ITC", "quantity": 35, "current_value": 9347.0, "pnl": -570.0},
    ],
    "summary": {"current_value": 32344.0, "holdings_count": 3, "overall_pnl": -147.0},
}


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height straight from the PNG IHDR chunk."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", data[16:24])


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def test_portfolio_chart_renders_a_real_png():
    width, height = png_size(charts.portfolio_chart(PORTFOLIO))
    assert width > 600 and height > 200


def test_empty_portfolio_renders_a_message_not_a_crash():
    width, _ = png_size(charts.portfolio_chart({"holdings": [], "summary": {}}))
    assert width > 200


def test_one_closed_position_is_a_stat_tile_not_a_one_bar_chart():
    """A single bar encodes nothing the number does not already say."""
    perf = {"available": True, "total_realized": 999.0, "unrealized": 0.0,
            "stats": {"win_rate": 100.0, "closed_trades": 1},
            "realized_by_symbol": {"ICICIBANK": 999.0}}
    tile = charts.performance_chart(perf)
    many = charts.performance_chart({**perf, "realized_by_symbol": {
        "ICICIBANK": 999.0, "TCS": 2400.0, "ITC": -820.0, "WIPRO": 1300.0}})
    # The tile is fixed-height; the bar chart grows with the number of rows.
    assert png_size(tile)[1] < png_size(many)[1]


def test_performance_unavailable_renders_a_message():
    assert png_size(charts.performance_chart({"available": False}))[0] > 200


def test_analysis_chart_renders_with_indicator_series():
    closes = [100 + (i % 7) for i in range(60)]
    history = {
        "dates": [f"{i} Jan" for i in range(60)],
        "close": closes,
        "bb_upper": [c + 5 for c in closes],
        "bb_lower": [c - 5 for c in closes],
        "rsi": [50 + (i % 20) - 10 for i in range(60)],
    }
    analysis = {"symbol": "TEST", "price": 103, "verdict": "BUY", "composite_score": 72,
                "risk": {"stop_loss": 95.0, "target_1": 110.0, "target_2": 118.0}}
    width, height = png_size(charts.analysis_chart(analysis, history))
    assert width > 800 and height > 400


def test_analysis_chart_without_history_is_a_message():
    assert png_size(charts.analysis_chart({"symbol": "X"}, {"close": []}))[0] > 200


# --------------------------------------------------------------------------- #
# money formatting — the sign must lead
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value, expected", [
    (1200, "₹1,200"),
    (-1200, "−₹1,200"),
    (0, "₹0"),
])
def test_negative_amounts_put_the_sign_before_the_symbol(value, expected):
    assert charts._rupees(value) == expected


# --------------------------------------------------------------------------- #
# through the tool surface
# --------------------------------------------------------------------------- #
def _blocks(server, name, **kwargs):
    result = asyncio.run(server.call_tool(name, kwargs))
    return result[0] if isinstance(result, tuple) else result


def _kinds(blocks):
    return [type(b).__name__ for b in blocks]


def test_view_portfolio_returns_a_chart_beside_the_data(mcp_with_holding):
    blocks = _blocks(mcp_with_holding, "view_portfolio")
    assert "ImageContent" in _kinds(blocks), "portfolio should come with a chart"
    text = next(b for b in blocks if getattr(b, "text", None))
    assert json.loads(text.text)["summary"]["holdings_count"] == 1


def test_chart_failure_still_returns_the_numbers(mcp_with_holding, monkeypatch):
    """A broken renderer must not cost the user their portfolio."""
    def boom(_):
        raise RuntimeError("matplotlib exploded")

    monkeypatch.setattr(charts, "portfolio_chart", boom)
    blocks = _blocks(mcp_with_holding, "view_portfolio")
    assert "ImageContent" not in _kinds(blocks)
    text = next(b for b in blocks if getattr(b, "text", None))
    assert json.loads(text.text)["summary"]["holdings_count"] == 1
