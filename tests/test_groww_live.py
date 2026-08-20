"""Live (Groww) broker path, exercised end-to-end against an in-memory fake SDK.

No network, no credentials, no real orders — but every mapping the real broker
performs (order params, response normalisation, LTP enrichment, auth-refresh
retry, fail-closed cap) is driven exactly as it would be against the real API.
"""
from __future__ import annotations

import json

import pytest

from trinetra.broker.base import OrderRequest, BrokerError


class FakeGrowwAPI:
    """Minimal stand-in for growwapi.GrowwAPI. Records calls; returns canned,
    realistically-shaped responses. Intentionally omits the EXCHANGE_*/SEGMENT_*
    constants so the broker's defensive `_const` fallback (literal value) is
    exercised too."""

    def __init__(self):
        self.calls = []

    def _record(self, name, **kw):
        self.calls.append((name, kw))

    def place_order(self, **kw):
        self._record("place_order", **kw)
        return {"groww_order_id": "GW-1001", "order_status": "OPEN"}

    def modify_order(self, **kw):
        self._record("modify_order", **kw)
        return {"groww_order_id": "GW-1001", "order_status": "MODIFIED"}

    def cancel_order(self, **kw):
        self._record("cancel_order", **kw)
        return {"groww_order_id": "GW-1001", "order_status": "CANCELLED"}

    def get_order_status(self, **kw):
        self._record("get_order_status", **kw)
        return {"groww_order_id": "GW-1001", "order_status": "OPEN",
                "order_type": "LIMIT", "quantity": 7}

    def get_order_list(self, **kw):
        self._record("get_order_list", **kw)
        return {"order_list": [
            {"trading_symbol": "RELIANCE", "transaction_type": "BUY", "quantity": 10,
             "price": 2500, "order_type": "LIMIT", "order_status": "EXECUTED",
             "created_at": "2026-06-30T10:00:00"},
        ]}

    def get_holdings_for_user(self, **kw):
        self._record("get_holdings_for_user", **kw)
        return {"holdings": [
            {"trading_symbol": "RELIANCE", "quantity": 10, "average_price": 2400.0},
            {"trading_symbol": "INFY", "quantity": 5, "average_price": 1500.0},
        ]}

    def get_positions_for_user(self, **kw):
        self._record("get_positions_for_user", **kw)
        return {"positions": [
            {"trading_symbol": "RELIANCE", "quantity": 10, "product": "CNC",
             "segment": "CASH", "net_price": 2400.0, "realised_pnl": 0.0},
        ]}

    def get_available_margin_details(self, **kw):
        self._record("get_available_margin_details", **kw)
        return {"clear_cash": 0.0,  # legitimate zero must be respected, not skipped
                "net_margin_used": 1234.5,
                "equity_margin_details": {"cnc_balance_available": 5000.0,
                                          "mis_balance_available": 2000.0}}

    def get_ltp(self, exchange_trading_symbols, segment, **kw):
        self._record("get_ltp", exchange_trading_symbols=exchange_trading_symbols, segment=segment)
        return {"NSE_RELIANCE": 2600.0, "NSE_INFY": 1450.0}


@pytest.fixture
def broker(monkeypatch):
    import trinetra.broker.groww_client as gc
    import trinetra.broker.groww_broker as gb
    import trinetra.market_data as market_data

    fake = FakeGrowwAPI()
    monkeypatch.setattr(gc, "get_client", lambda force_refresh=False: fake)
    monkeypatch.setattr(market_data, "try_ltp", lambda s: 2600.0)
    b = gb.GrowwBroker()
    b._fake = fake
    return b


# --- order placement ---------------------------------------------------------
def test_market_order_maps_and_normalises(broker):
    req = OrderRequest(trading_symbol="reliance.ns", transaction_type="buy",
                       quantity=3, order_type="market")
    res = broker.place_order(req, reference_price=2600.0)
    assert res.status == "open"
    assert res.order_id == "GW-1001"
    assert res.trading_symbol == "RELIANCE"
    assert res.exchange == "NSE"
    name, kw = broker._fake.calls[-1]
    assert name == "place_order"
    assert kw["trading_symbol"] == "RELIANCE"
    assert kw["order_type"] == "MARKET"
    assert kw["transaction_type"] == "BUY"
    assert kw["price"] == 0.0  # market orders send price 0


def test_market_order_fetches_reference_price_when_missing(broker):
    """If the caller passes no reference price, the live broker fetches one so the
    cap is enforced — and the order still goes through (try_ltp stubbed to 2600)."""
    req = OrderRequest(trading_symbol="RELIANCE", transaction_type="BUY",
                       quantity=2, order_type="MARKET")
    res = broker.place_order(req, reference_price=None)
    assert res.status == "open"
    assert res.estimated_value == 5200.0  # 2 * 2600


def test_limit_order_sends_price(broker):
    req = OrderRequest(trading_symbol="RELIANCE", transaction_type="SELL",
                       quantity=4, order_type="LIMIT", price=2750.0)
    res = broker.place_order(req, reference_price=None)
    assert res.status == "open"
    name, kw = broker._fake.calls[-1]
    assert kw["price"] == 2750.0
    assert kw["order_type"] == "LIMIT"


def test_stop_loss_order_sends_trigger(broker):
    req = OrderRequest(trading_symbol="RELIANCE", transaction_type="SELL", quantity=4,
                       order_type="SL", price=2400.0, trigger_price=2410.0)
    broker.place_order(req, reference_price=None)
    name, kw = broker._fake.calls[-1]
    assert kw["order_type"] == "STOP_LOSS"
    assert kw["trigger_price"] == 2410.0
    assert kw["price"] == 2400.0


def test_over_cap_blocked_before_reaching_sdk(broker, set_cap):
    set_cap(100_000.0)
    req = OrderRequest(trading_symbol="RELIANCE", transaction_type="BUY",
                       quantity=1000, order_type="LIMIT", price=2500.0)  # 2.5M
    with pytest.raises(BrokerError, match="exceeds the safety cap"):
        broker.place_order(req, reference_price=None)
    # SDK must NOT have been called.
    assert all(c[0] != "place_order" for c in broker._fake.calls)


# --- modify / cancel / status / history -------------------------------------
def test_modify_uses_existing_quantity_when_not_given(broker):
    out = broker.modify_order("GW-1001", price=2600.0)
    assert out["status"] == "MODIFIED"
    # broker first reads status (qty=7) then modifies with that qty.
    modify = [c for c in broker._fake.calls if c[0] == "modify_order"][0][1]
    assert modify["quantity"] == 7
    assert modify["price"] == 2600.0


def test_cancel(broker):
    out = broker.cancel_order("GW-1001")
    assert out["status"] == "CANCELLED"
    assert out["order_id"] == "GW-1001"


def test_order_history_normalised(broker):
    orders = broker.get_order_history(limit=5)
    assert len(orders) == 1
    assert orders[0]["trading_symbol"] == "RELIANCE"


# --- portfolio ---------------------------------------------------------------
def test_holdings_enriched_with_ltp(broker):
    holdings = broker.get_holdings()
    by_sym = {h.trading_symbol: h for h in holdings}
    assert by_sym["RELIANCE"].last_price == 2600.0
    assert by_sym["RELIANCE"].invested == 24000.0
    assert by_sym["RELIANCE"].current_value == 26000.0
    assert by_sym["RELIANCE"].pnl == 2000.0
    assert by_sym["INFY"].last_price == 1450.0


def test_positions(broker):
    positions = broker.get_positions()
    assert positions[0].trading_symbol == "RELIANCE"
    assert positions[0].quantity == 10


def test_funds_respects_legit_zero_clear_cash(broker):
    f = broker.get_funds().to_dict()
    assert f["available_cash"] == 0.0      # the real zero, not the 5000 fallback
    assert f["margin_used"] == 1234.5


# --- auth-refresh retry ------------------------------------------------------
def test_auth_error_triggers_single_reauth_then_succeeds(monkeypatch):
    import trinetra.broker.groww_client as gc
    import trinetra.broker.groww_broker as gb
    import trinetra.market_data as market_data

    class AuthenticationError(Exception):
        pass

    good = FakeGrowwAPI()

    class FlakyOnce(FakeGrowwAPI):
        def __init__(self):
            super().__init__()
            self.failed = False

        def get_holdings_for_user(self, **kw):
            if not self.failed:
                self.failed = True
                raise AuthenticationError("session expired")
            return super().get_holdings_for_user(**kw)

    flaky = FlakyOnce()
    clients = [flaky, good]

    def fake_get_client(force_refresh=False):
        return clients[1] if force_refresh else clients[0]

    monkeypatch.setattr(gc, "get_client", fake_get_client)
    monkeypatch.setattr(gc, "reset_client", lambda: None)
    monkeypatch.setattr(market_data, "try_ltp", lambda s: 2600.0)

    b = gb.GrowwBroker()
    holdings = b.get_holdings()  # first call 401s, retries on a fresh client, succeeds
    assert len(holdings) == 2


def test_result_to_dict_is_json_serialisable(broker):
    req = OrderRequest(trading_symbol="RELIANCE", transaction_type="BUY",
                       quantity=2, order_type="MARKET")
    res = broker.place_order(req, reference_price=2600.0)
    json.dumps(res.to_dict())  # must not raise
    assert "raw" not in res.to_dict()
