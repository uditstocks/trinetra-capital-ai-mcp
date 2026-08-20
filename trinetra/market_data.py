"""Market data layer.

Live prices come from Groww when the account is connected (real-time, the same
feed the orders execute against); everything falls back to yfinance when Groww
is not configured or a call fails, so research/sentiment keep working even
before the user connects their broker.

    get_live_quote(symbol)   -> dict   (price + day stats, Groww-first)
    try_ltp(symbol)          -> float|None  (cheap last-traded price)
    fetch_fundamentals(sym)  -> dict   (name/sector/PE/marketcap/52w via yfinance)
    lookup_symbol(name)      -> dict   (company name -> Groww trading symbol)
    technical_snapshot(sym)  -> dict   (RSI/MACD/Bollinger/ATR + sentiment)
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf
from textblob import TextBlob

from trinetra.config import settings
from trinetra.logging_setup import get_logger
from trinetra import instruments
from trinetra.symbols import Instrument, normalize


def _inst(symbol: str) -> Instrument:
    """Resolve a user/agent-supplied symbol or name to a canonical instrument via
    the Groww master (authoritative), falling back to raw normalization."""
    return instruments.to_instrument(symbol)

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Groww access (optional — only used when credentials are present)
# --------------------------------------------------------------------------- #
def _groww():
    """Return an authenticated Groww client, or None if unavailable."""
    if not settings.groww_configured:
        return None
    try:
        from trinetra.broker import groww_client

        return groww_client.get_client()
    except Exception as exc:  # noqa: BLE001 - data layer must never hard-fail
        log.debug("Groww live data unavailable, using fallback: %s", exc)
        return None


# Short-lived LTP cache so a single portfolio view (or repeated quote within a
# turn) doesn't hit the data source once per symbol per call. Prices are still
# "live" — just deduplicated within a few seconds.
_LTP_TTL = 10.0  # seconds
_ltp_cache: dict[str, tuple[float, float]] = {}  # exchange_token -> (price, ts)


def _cache_get(token: str) -> float | None:
    hit = _ltp_cache.get(token)
    if hit and (time.monotonic() - hit[1]) < _LTP_TTL:
        return hit[0]
    return None


def _cache_put(token: str, price: float) -> None:
    _ltp_cache[token] = (price, time.monotonic())


def _finite(x: Any) -> float | None:
    """Return x as a float only if it is a real, finite number; else None.
    Guards against yfinance/Groww occasionally yielding NaN/inf which would
    serialise to invalid JSON and confuse the LLM."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _seg(client, value: str) -> str:
    return getattr(client, f"SEGMENT_{value}", value)


def _exch(client, value: str) -> str:
    return getattr(client, f"EXCHANGE_{value}", value)


# --------------------------------------------------------------------------- #
# live quotes
# --------------------------------------------------------------------------- #
def try_ltp(symbol: str) -> float | None:
    """Cheapest possible last-traded-price lookup; None if unavailable. Cached
    for a few seconds to avoid duplicate calls within a turn."""
    inst = _inst(symbol)
    cached = _cache_get(inst.exchange_token)
    if cached is not None:
        return cached

    client = _groww()
    if client is not None:
        try:
            resp = client.get_ltp(
                exchange_trading_symbols=(inst.exchange_token,),
                segment=_seg(client, "CASH"),
            )
            for v in (resp or {}).values():
                px = _finite(v)
                if px is not None:
                    _cache_put(inst.exchange_token, px)
                    return px
        except Exception as exc:  # noqa: BLE001
            log.debug("Groww LTP failed for %s: %s", inst, exc)
    # yfinance fallback
    try:
        hist = yf.Ticker(inst.yf_symbol).history(period="5d").dropna(subset=["Close"])
        if not hist.empty:
            px = _finite(hist["Close"].iloc[-1])
            if px is not None:
                px = round(px, 2)
                _cache_put(inst.exchange_token, px)
                return px
    except Exception as exc:  # noqa: BLE001
        log.debug("yfinance LTP failed for %s: %s", inst, exc)
    return None


def ltp_many(symbols: list[str]) -> dict[str, float]:
    """Batch LTP for several symbols, keyed by the bare trading symbol.

    Uses Groww's batched get_ltp (up to 50/call) when connected; otherwise falls
    back to per-symbol yfinance lookups. Honours the short-lived cache.
    """
    out: dict[str, float] = {}
    insts = [_inst(s) for s in symbols]
    pending = []
    for inst in insts:
        cached = _cache_get(inst.exchange_token)
        if cached is not None:
            out[inst.trading_symbol] = cached
        else:
            pending.append(inst)

    client = _groww()
    if client is not None and pending:
        tokens = [i.exchange_token for i in pending]
        for i in range(0, len(tokens), 50):
            chunk = tuple(tokens[i : i + 50])
            try:
                resp = client.get_ltp(
                    exchange_trading_symbols=chunk, segment=_seg(client, "CASH")
                )
                for token, v in (resp or {}).items():
                    px = _finite(v)
                    if px is None:
                        continue
                    _cache_put(token, px)
                    # token looks like "NSE_RELIANCE" -> bare symbol
                    bare = token.split("_", 1)[-1]
                    out[bare] = px
            except Exception as exc:  # noqa: BLE001
                log.debug("Groww batch LTP failed: %s", exc)
        pending = [i for i in pending if i.trading_symbol not in out]

    # yfinance fallback for whatever's left
    for inst in pending:
        px = try_ltp(inst.trading_symbol)
        if px is not None:
            out[inst.trading_symbol] = px
    return out


def get_live_quote(symbol: str) -> dict[str, Any]:
    """Real-time quote, Groww-first with a yfinance fallback."""
    inst = _inst(symbol)
    client = _groww()
    if client is not None:
        try:
            q = client.get_quote(
                trading_symbol=inst.trading_symbol,
                exchange=_exch(client, inst.exchange),
                segment=_seg(client, "CASH"),
            )
            ohlc = q.get("ohlc", {}) or {}
            return {
                "source": "groww",
                "symbol": inst.trading_symbol,
                "exchange": inst.exchange,
                "last_price": q.get("last_price"),
                "day_change": q.get("day_change"),
                "day_change_perc": q.get("day_change_perc"),
                "open": ohlc.get("open"),
                "high": ohlc.get("high"),
                "low": ohlc.get("low"),
                "prev_close": ohlc.get("close"),
                "volume": q.get("volume"),
                "week_52_high": q.get("week_52_high"),
                "week_52_low": q.get("week_52_low"),
                "upper_circuit": q.get("upper_circuit_limit"),
                "lower_circuit": q.get("lower_circuit_limit"),
            }
        except Exception as exc:  # noqa: BLE001
            log.debug("Groww quote failed for %s, falling back: %s", inst, exc)

    return _yf_quote(inst)


def _yf_quote(inst: Instrument) -> dict[str, Any]:
    try:
        tk = yf.Ticker(inst.yf_symbol)
        hist = tk.history(period="5d").dropna(subset=["Close"])
        if hist.empty or _finite(hist["Close"].iloc[-1]) is None:
            return {
                "source": "yfinance",
                "symbol": inst.trading_symbol,
                "error": "no price data available (market data source returned empty/NaN)",
            }
        last = round(float(hist["Close"].iloc[-1]), 2)
        prev = round(float(hist["Close"].iloc[-2]), 2) if len(hist) > 1 else last
        return {
            "source": "yfinance",
            "symbol": inst.trading_symbol,
            "exchange": inst.exchange,
            "last_price": last,
            "prev_close": prev,
            "day_change": round(last - prev, 2),
            "day_change_perc": round((last - prev) / prev * 100, 2) if prev else None,
            "high": round(float(hist["High"].max()), 2),
            "low": round(float(hist["Low"].min()), 2),
        }
    except Exception as exc:  # noqa: BLE001
        return {"source": "yfinance", "symbol": inst.trading_symbol, "error": str(exc)}


# --------------------------------------------------------------------------- #
# fundamentals + symbol lookup (yfinance — Groww has no fundamentals endpoint)
# --------------------------------------------------------------------------- #
def fetch_fundamentals(symbol: str) -> dict[str, Any]:
    inst = _inst(symbol)
    try:
        info = yf.Ticker(inst.yf_symbol).info
    except Exception as exc:  # noqa: BLE001
        return {"symbol": inst.trading_symbol, "error": str(exc)}
    return {
        "symbol": inst.trading_symbol,
        "exchange": inst.exchange,
        "company_name": info.get("longName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap": info.get("marketCap"),
        "pe_ratio": info.get("trailingPE"),
        "52w_high": info.get("fiftyTwoWeekHigh"),
        "52w_low": info.get("fiftyTwoWeekLow"),
    }


def lookup_symbol(company_name: str) -> dict[str, Any]:
    """Resolve a company name/ticker to its exact Groww trading symbol using the
    Groww instrument master (authoritative). Returns the best match plus close
    alternatives. Falls back to yfinance search only if the master is unavailable."""
    exchange = None
    # Match the exchange hint only as a whole word, so a ticker that merely
    # contains "nse"/"bse" as a substring (or the listed company "BSE Ltd")
    # isn't mistaken for an exchange qualifier.
    if re.search(r"\bbse\b", company_name, flags=re.IGNORECASE):
        exchange = "BSE"
    elif re.search(r"\bnse\b", company_name, flags=re.IGNORECASE):
        exchange = "NSE"
    # Strip the exchange word (whole word only) so it doesn't pollute name matching.
    query = re.sub(r"\b(?:nse|bse)\b", " ", company_name, flags=re.IGNORECASE).strip()

    matches = instruments.search(query or company_name, limit=5, exchange=exchange)
    if matches:
        best = matches[0]
        return {
            "query": company_name,
            "trading_symbol": best.trading_symbol,
            "exchange": best.exchange,
            "name": best.name,
            "isin": best.isin or None,
            "lot_size": best.lot_size,
            "source": "groww_instruments",
            "alternatives": [
                {"trading_symbol": m.trading_symbol, "exchange": m.exchange, "name": m.name}
                for m in matches[1:]
            ],
        }

    # Fallback: yfinance search (only if the instrument master couldn't load).
    try:
        results = yf.Search(company_name, max_results=10).quotes
    except Exception as exc:  # noqa: BLE001
        return {"query": company_name, "error": f"no match (instrument master unavailable): {exc}"}
    if not results:
        return {"query": company_name, "error": "no match"}
    chosen = next((r for r in results if r.get("symbol", "").endswith((".NS", ".BO"))), results[0])
    inst = normalize(chosen["symbol"])
    return {
        "query": company_name,
        "trading_symbol": inst.trading_symbol,
        "exchange": inst.exchange,
        "name": chosen.get("longname") or chosen.get("shortname"),
        "source": "yfinance_fallback",
    }


# --------------------------------------------------------------------------- #
# technical + news sentiment snapshot (yfinance history + TextBlob)
# --------------------------------------------------------------------------- #
def technical_snapshot(symbol: str) -> dict[str, Any]:
    inst = _inst(symbol)
    try:
        hist = yf.Ticker(inst.yf_symbol).history(period="90d", interval="1d")
        hist = hist.dropna(subset=["Close", "High", "Low"])
    except Exception as exc:  # noqa: BLE001
        return {"symbol": inst.trading_symbol, "error": f"data fetch failed: {exc}"}

    if hist.empty or len(hist) < 30:
        return {"symbol": inst.trading_symbol, "error": "not enough clean price history"}

    close, high, low = hist["Close"], hist["High"], hist["Low"]

    def _last(series, default: float = 0.0) -> float:
        """Last value of a series as a real, finite float (never NaN/inf)."""
        try:
            val = _finite(series.iloc[-1])
        except (IndexError, KeyError):
            return default
        return val if val is not None else default

    # RSI-14 — handle the all-gains / all-losses edges explicitly so we never
    # divide by zero into a NaN (a pure uptrend is RSI 100, a flat series is 50).
    delta = close.diff()
    avg_gain = delta.clip(lower=0).ewm(com=13, min_periods=14).mean()
    avg_loss = (-delta.clip(upper=0)).ewm(com=13, min_periods=14).mean()
    last_gain = _last(avg_gain)
    last_loss = _last(avg_loss)
    if last_loss <= 0:
        rsi = 100.0 if last_gain > 0 else 50.0
    else:
        rsi = 100 - 100 / (1 + last_gain / last_loss)
    rsi = round(rsi, 2)

    # MACD histogram
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = round(_last(macd_line - signal_line), 4)

    # Bollinger %B (0 = at lower band, 1 = at upper band). A flat window has zero
    # width, so default to the mid-band (0.5) instead of producing inf/NaN.
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    width = _last(4 * std20)
    pct_b = round(_last(close - (sma20 - 2 * std20)) / width, 3) if width > 0 else 0.5

    # ATR-14
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    atr = round(_last(tr.ewm(com=13, min_periods=14).mean()), 4)

    # Prefer the real Groww live price; fall back to yfinance close.
    price = try_ltp(symbol) or round(_last(close), 2)

    headlines = _fetch_headlines(inst)
    scores = [TextBlob(h).sentiment.polarity for h in headlines] if headlines else [0.0]
    avg_sent = round(float(np.mean(scores)), 3)
    sent_label = "bullish" if avg_sent > 0.15 else "bearish" if avg_sent < -0.15 else "neutral"

    # Composite score. Each contribution is captured as it is applied so callers
    # can report exactly how the verdict was reached instead of re-deriving it.
    breakdown: list[dict[str, Any]] = []

    def _score(indicator: str, value: Any, reading: str, points: int, note: str) -> int:
        breakdown.append({
            "indicator": indicator,
            "value": value,
            "reading": reading,
            "points": points,
            "note": note,
        })
        return points

    score = 50
    if rsi < 30:
        score += _score("RSI-14", rsi, "oversold", 20,
                        "Selling looks exhausted — historically a bounce zone.")
    elif rsi < 40:
        score += _score("RSI-14", rsi, "leaning oversold", 10,
                        "Momentum is weak but not yet extreme.")
    elif rsi > 70:
        score += _score("RSI-14", rsi, "overbought", -20,
                        "Buying looks stretched — pullback risk is elevated.")
    elif rsi > 60:
        score += _score("RSI-14", rsi, "leaning overbought", -10,
                        "Momentum is strong but starting to look rich.")
    else:
        score += _score("RSI-14", rsi, "neutral", 0,
                        "Momentum is balanced; no edge either way.")

    if histogram > 0:
        score += _score("MACD histogram", histogram, "bullish crossover", 15,
                        "Short-term trend is pulling above the longer-term trend.")
    else:
        score += _score("MACD histogram", histogram, "bearish crossover", -15,
                        "Short-term trend is slipping below the longer-term trend.")

    if pct_b < 0.2:
        score += _score("Bollinger %B", pct_b, "near lower band", 10,
                        "Price sits at the cheap end of its recent range.")
    elif pct_b > 0.8:
        score += _score("Bollinger %B", pct_b, "near upper band", -10,
                        "Price is extended against its recent range.")
    else:
        score += _score("Bollinger %B", pct_b, "mid-band", 0,
                        "Price is mid-range — no stretch in either direction.")

    score += _score("News sentiment", avg_sent, sent_label, round(avg_sent * 15),
                    f"Polarity across {len(headlines)} recent headline(s).")

    raw_score = score
    score = max(0, min(100, score))

    action = "BUY" if score >= 65 else "SELL" if score <= 35 else "HOLD"
    confidence = "high" if score >= 80 or score <= 20 else "moderate"

    return {
        "score_base": 50,
        "score_breakdown": breakdown,
        "score_raw": raw_score,
        "headlines": headlines[:5],
        "symbol": inst.trading_symbol,
        "exchange": inst.exchange,
        "price": price,
        "rsi": rsi,
        "rsi_signal": "oversold" if rsi < 30 else "overbought" if rsi > 70 else "neutral",
        "macd_crossover": "bullish" if histogram > 0 else "bearish",
        "macd_histogram": histogram,
        "bollinger_pct_b": pct_b,
        "atr": atr,
        "sentiment_score": avg_sent,
        "sentiment_label": sent_label,
        "headlines_used": len(headlines),
        "composite_score": score,
        "signal": action,
        "confidence": confidence,
        "stop_loss": round(price - 1.5 * atr, 2),
        "target_1": round(price + 2.0 * atr, 2),
        "target_2": round(price + 3.5 * atr, 2),
    }


def _fetch_headlines(inst: Instrument) -> list[str]:
    """Recent headlines for an instrument, via yfinance's news API.

    Replaces an HTML scrape of Yahoo's quote page, which now returns 404 for
    every ticker. Best-effort throughout: when no headlines are available the
    sentiment component simply contributes 0 to the composite score rather than
    failing the analysis.
    """
    try:
        items = yf.Ticker(inst.yf_symbol).news or []
    except Exception as exc:  # noqa: BLE001 - sentiment is best-effort
        log.debug("News fetch failed for %s: %s", inst, exc)
        return []

    headlines: list[str] = []
    for item in items[:10]:
        # yfinance returns either a flat dict or {"content": {...}} by version.
        content = item.get("content") or item
        title = content.get("title") if isinstance(content, dict) else None
        if isinstance(title, str) and len(title.strip()) > 20:
            headlines.append(title.strip())
    return headlines
