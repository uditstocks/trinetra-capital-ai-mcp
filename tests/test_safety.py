"""The money-loss safety controls: order validation, notional estimation, and
the per-order rupee cap (the system's circuit breaker)."""
from __future__ import annotations

import pytest

from trinetra.broker.base import OrderRequest, BrokerError
from trinetra.broker.paper_broker import PaperBroker


def _req(**kw):
    base = dict(trading_symbol="RELIANCE", transaction_type="BUY", quantity=10)
    base.update(kw)
    return OrderRequest(**base)


# --- normalised() validation -------------------------------------------------
def test_normalise_canonicalises_symbol_and_exchange():
    r = _req(trading_symbol="reliance.ns").normalised()
    assert r.trading_symbol == "RELIANCE"
    assert r.exchange == "NSE"
    assert r.transaction_type == "BUY"


def test_normalise_rejects_bad_transaction_type():
    with pytest.raises(BrokerError):
        _req(transaction_type="hodl").normalised()


def test_normalise_rejects_nonpositive_quantity():
    with pytest.raises(BrokerError):
        _req(quantity=0).normalised()


def test_limit_requires_price():
    with pytest.raises(BrokerError):
        _req(order_type="LIMIT", price=0).normalised()


def test_sl_requires_trigger():
    with pytest.raises(BrokerError):
        _req(order_type="SL", price=100, trigger_price=0).normalised()


# --- estimated_value ---------------------------------------------------------
def test_estimated_value_market_uses_reference_price():
    assert _req(order_type="MARKET", quantity=10).estimated_value(reference_price=1500) == 15000.0


def test_estimated_value_limit_uses_limit_price():
    assert _req(order_type="LIMIT", quantity=10, price=1450).estimated_value() == 14500.0


def test_estimated_value_slm_uses_trigger():
    assert _req(order_type="SL_M", quantity=5, trigger_price=200).estimated_value() == 1000.0


# --- guard_order: the cap ----------------------------------------------------
def test_guard_rejects_over_cap(set_cap):
    set_cap(100_000.0)
    b = PaperBroker()
    with pytest.raises(BrokerError, match="exceeds the safety cap"):
        b.guard_order(_req(quantity=1000).normalised(), reference_price=2500)  # 2.5M


def test_guard_allows_under_cap(set_cap):
    set_cap(100_000.0)
    PaperBroker().guard_order(_req(quantity=10).normalised(), reference_price=2500)  # 25k ok


def test_guard_fails_closed_when_no_price(set_cap):
    """CRITICAL: a market order whose value can't be determined must be REJECTED,
    never allowed through uncapped."""
    set_cap(100_000.0)
    b = PaperBroker()
    with pytest.raises(BrokerError, match="Cannot determine"):
        b.guard_order(_req(order_type="MARKET", quantity=10).normalised(), reference_price=None)


def test_guard_boundary_equal_cap_passes(set_cap):
    set_cap(100_000.0)
    PaperBroker().guard_order(_req(quantity=10).normalised(), reference_price=10_000)  # ==cap
