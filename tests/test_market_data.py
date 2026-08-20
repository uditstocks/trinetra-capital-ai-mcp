"""Market-data layer: technical-indicator robustness (no NaN leaks into JSON)
and whole-word exchange handling in symbol lookup."""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

import trinetra.market_data as market_data
from trinetra.symbols import normalize


class _FakeTicker:
    def __init__(self, closes):
        idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
        self._df = pd.DataFrame(
            {"Close": closes,
             "High": [c * 1.01 for c in closes],
             "Low": [c * 0.99 for c in closes]},
            index=idx,
        )

    def history(self, *a, **kw):
        return self._df


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    # Resolve symbols without the Groww instrument master / network.
    monkeypatch.setattr(market_data, "_inst", lambda s: normalize(s))
    monkeypatch.setattr(market_data, "try_ltp", lambda s: None)
    monkeypatch.setattr(market_data, "_fetch_headlines", lambda inst: [])
    market_data._ltp_cache.clear()


def test_technical_snapshot_pure_uptrend_no_nan(monkeypatch):
    closes = [100 + i for i in range(90)]  # strictly increasing -> all gains
    monkeypatch.setattr(market_data.yf, "Ticker", lambda s: _FakeTicker(closes))
    snap = market_data.technical_snapshot("RELIANCE")
    assert "error" not in snap
    # RSI must be a real number (the old code produced NaN here).
    assert snap["rsi"] == 100.0
    assert math.isfinite(snap["rsi"])
    assert math.isfinite(snap["bollinger_pct_b"])
    assert math.isfinite(snap["atr"])
    # Result must be strict-JSON serialisable (no NaN/inf tokens).
    txt = json.dumps(snap)
    assert "NaN" not in txt and "Infinity" not in txt


def test_technical_snapshot_flat_series_is_neutral(monkeypatch):
    closes = [100.0] * 90  # no movement
    monkeypatch.setattr(market_data.yf, "Ticker", lambda s: _FakeTicker(closes))
    snap = market_data.technical_snapshot("RELIANCE")
    assert snap["rsi"] == 50.0           # flat -> neutral, not NaN
    assert snap["bollinger_pct_b"] == 0.5  # zero band width -> mid
    assert json.dumps(snap)


def test_lookup_symbol_whole_word_exchange(monkeypatch):
    captured = {}

    def fake_search(query, limit=5, exchange=None):
        captured["query"] = query
        captured["exchange"] = exchange
        return []

    monkeypatch.setattr(market_data.instruments, "search", fake_search)
    monkeypatch.setattr(market_data.yf, "Search", lambda *a, **k: type("S", (), {"quotes": []})())

    market_data.lookup_symbol("RELIANCE NSE")
    assert captured["query"] == "RELIANCE"
    assert captured["exchange"] == "NSE"


def test_lookup_symbol_substring_not_treated_as_exchange(monkeypatch):
    captured = {}

    def fake_search(query, limit=5, exchange=None):
        captured["query"] = query
        captured["exchange"] = exchange
        return []

    monkeypatch.setattr(market_data.instruments, "search", fake_search)
    monkeypatch.setattr(market_data.yf, "Search", lambda *a, **k: type("S", (), {"quotes": []})())

    # "TRANSENSE" contains "nse" as a substring but is NOT an exchange qualifier.
    market_data.lookup_symbol("TRANSENSE")
    assert captured["query"] == "TRANSENSE"
    assert captured["exchange"] is None
