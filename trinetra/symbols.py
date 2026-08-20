"""Symbol normalisation between yfinance and Groww.

yfinance speaks suffixed symbols:  RELIANCE.NS  /  RELIANCE.BO
Groww speaks the bare trading symbol plus an explicit exchange:
        trading_symbol="RELIANCE", exchange="NSE"
and, for batch live-data calls, a combined token:  "NSE_RELIANCE".

These helpers are intentionally pure (no network) so they are cheap and
testable. For the rare ticker whose Groww trading symbol differs from the NSE
ticker, the optional instrument-master lookup (see `market_data.resolve_symbol`)
can be layered on top.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from trinetra.config import settings

NSE = "NSE"
BSE = "BSE"

# Exchange prefix in any common style: "NSE:RELIANCE", "NSE_RELIANCE",
# "NSE RELIANCE", "NSE-RELIANCE" (TradingView / Groww token / LLM shorthand).
_EXCHANGE_PREFIX = re.compile(r"^(NSE|BSE)[\s:_\-]+(.+)$")


@dataclass(frozen=True)
class Instrument:
    trading_symbol: str  # Groww/NSE bare symbol, e.g. "RELIANCE"
    exchange: str        # "NSE" or "BSE"

    @property
    def yf_symbol(self) -> str:
        suffix = ".BO" if self.exchange == BSE else ".NS"
        return f"{self.trading_symbol}{suffix}"

    @property
    def exchange_token(self) -> str:
        """The "NSE_RELIANCE" token used by get_ltp / get_ohlc."""
        return f"{self.exchange}_{self.trading_symbol}"

    def __str__(self) -> str:
        return f"{self.trading_symbol}@{self.exchange}"


def normalize(symbol: str, exchange: str | None = None) -> Instrument:
    """Turn any of these into a canonical Instrument:

        "RELIANCE"        -> RELIANCE@NSE   (default exchange)
        "reliance.ns"     -> RELIANCE@NSE
        "TCS.BO"          -> TCS@BSE
        "NSE_INFY"        -> INFY@NSE
        "NSE:RELIANCE"    -> RELIANCE@NSE   (TradingView / LLM shorthand)
        "NSE:RELIANCE.NS" -> RELIANCE@NSE
        ("WIPRO","BSE")   -> WIPRO@BSE
    """
    if not symbol:
        raise ValueError("empty symbol")

    s = symbol.strip().upper()
    ex = (exchange or "").strip().upper() or None

    # Strip an exchange prefix ("NSE:RELIANCE", "NSE_INFY", "BSE RELIANCE") so the
    # prefix never leaks into the yfinance symbol (the "NSE:RELIANCE.NS" 404 bug).
    m = _EXCHANGE_PREFIX.match(s)
    if m:
        ex = ex or m.group(1)
        s = m.group(2).strip()

    # yfinance suffixes (an explicit suffix wins over the prefix/default).
    if s.endswith(".NS"):
        return Instrument(s[:-3], NSE)
    if s.endswith(".BO"):
        return Instrument(s[:-3], BSE)

    # bare symbol — use explicit exchange, else the configured default
    return Instrument(s, ex or settings.default_exchange)


def to_groww(symbol: str, exchange: str | None = None) -> tuple[str, str]:
    """Convenience: returns (trading_symbol, exchange) for Groww calls."""
    inst = normalize(symbol, exchange)
    return inst.trading_symbol, inst.exchange


def to_yf(symbol: str, exchange: str | None = None) -> str:
    """Convenience: returns the yfinance-style symbol."""
    return normalize(symbol, exchange).yf_symbol
