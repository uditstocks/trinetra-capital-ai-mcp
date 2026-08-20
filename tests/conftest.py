"""Shared pytest fixtures.

These tests are fully offline and never touch a real broker or real money:
- the paper portfolio is redirected to a temp file,
- the Groww SDK is replaced by an in-memory fake,
- market-data network calls are stubbed.
"""
from __future__ import annotations

import pytest

from trinetra.config import settings


@pytest.fixture(autouse=True)
def _local_backend_by_default(monkeypatch):
    """Run every test against the file backend unless it asks for a database.

    Without this, a developer (or a CI job) with DATABASE_URL exported would
    silently exercise a different storage path than the one the test intends.
    Tests that want Postgres/SQLite set DATABASE_URL themselves.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _force_set(obj, **attrs):
    """Mutate a frozen dataclass instance in place (object.__setattr__ bypasses
    the frozen guard). Returns the originals so a fixture can restore them."""
    originals = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        object.__setattr__(obj, k, v)
    return originals


@pytest.fixture
def isolate_portfolio(tmp_path):
    """Point the paper portfolio at a throwaway file and reset broker state."""
    import trinetra.broker as broker_pkg
    import trinetra.market_data as market_data

    pfile = tmp_path / "portfolio.json"
    originals = _force_set(settings, portfolio_file=pfile)
    broker_pkg._broker = None
    market_data._ltp_cache.clear()
    yield pfile
    for k, v in originals.items():
        object.__setattr__(settings, k, v)
    broker_pkg._broker = None
    market_data._ltp_cache.clear()


@pytest.fixture
def set_cap():
    """Factory to temporarily override the per-order safety cap."""
    saved = {}

    def _apply(value: float):
        saved.update(_force_set(settings, max_order_value=value))

    yield _apply
    for k, v in saved.items():
        object.__setattr__(settings, k, v)


# --------------------------------------------------------------------------- #
# MCP server fixtures
# --------------------------------------------------------------------------- #
def _instrument(symbol="RELIANCE", exchange="NSE", buy=True):
    from trinetra.instruments import InstrumentRecord

    return InstrumentRecord(
        trading_symbol=symbol, exchange=exchange, name=f"{symbol} Ltd",
        series="EQ", isin="INE000000001", lot_size=1,
        buy_allowed=buy, sell_allowed=True,
    )


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    """An MCP server on an isolated data root, with the network stubbed out."""
    import trinetra.market_data as market_data
    from trinetra import broker as broker_pkg
    from trinetra.services import trading as trading_svc
    from trinetra_mcp import runtime
    from trinetra_mcp.server import build_server

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TRINETRA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRINETRA_USER_ID", "tester")
    monkeypatch.setattr(trading_svc.instruments, "resolve", lambda s, e=None: _instrument())
    monkeypatch.setattr(trading_svc.instruments, "search", lambda s, limit=3: [])
    monkeypatch.setattr(market_data, "try_ltp", lambda s: 2500.0)
    monkeypatch.setattr(market_data, "ltp_many", lambda syms: dict.fromkeys(syms, 2600.0))
    broker_pkg.reset_brokers()
    runtime.clear_tokens()
    yield build_server()
    broker_pkg.reset_brokers()
    runtime.clear_tokens()


@pytest.fixture
def mcp_with_holding(mcp):
    """The same server, already set up and holding one filled position."""
    import asyncio
    import json

    def call(name, **kwargs):
        result = asyncio.run(mcp.call_tool(name, kwargs))
        content = result[0] if isinstance(result, tuple) else result
        text = next(b for b in content if getattr(b, "text", None))
        return json.loads(text.text)

    call("setup_account", mode="paper")
    preview = call("place_order", symbol="RELIANCE", action="buy", quantity=4)
    call("confirm_order", confirmation_token=preview["confirmation_token"])
    return mcp
