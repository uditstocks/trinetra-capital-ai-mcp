"""Shared pytest fixtures.

These tests are fully offline and never touch a real broker or real money:
- the paper portfolio is redirected to a temp file,
- the Groww SDK is replaced by an in-memory fake,
- market-data network calls are stubbed.
"""
from __future__ import annotations

import pytest

from trinetra.config import settings


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
