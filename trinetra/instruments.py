"""Groww instrument master — authoritative symbol resolution.

The single source of truth for what Groww can actually trade. Downloads Groww's
public instrument CSV (no auth required), caches it locally, refreshes daily, and
builds an in-memory index to resolve a company name OR ticker into the EXACT
Groww trading symbol + exchange.

This replaces unreliable yfinance symbol guessing that produced dead tickers like
"INFOSYS.NS" (correct: INFY) or "PHYSICSWALLAH.NS" (correct: PWL).

    resolve("infosys")        -> InstrumentRecord(INFY @ NSE)
    resolve("physicswallah")  -> InstrumentRecord(PWL  @ NSE)
    search("icici", limit=5)  -> [ICICIBANK, ICICIGI, ICICIPRULI, ...]
    to_instrument("INFOSYS")  -> symbols.Instrument(INFY, NSE)   # drop-in for normalize()
"""

from __future__ import annotations

import csv
import io
import re
import threading
import time
from dataclasses import dataclass

import requests

from trinetra.config import PROJECT_ROOT
from trinetra.logging_setup import get_logger
from trinetra.symbols import BSE, NSE, Instrument, normalize

log = get_logger(__name__)

CSV_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"
CACHE_FILE = PROJECT_ROOT / ".groww_instruments.csv"
MAX_AGE_SECONDS = 86_400  # refresh once a day

_index: dict | None = None          # {"by_symbol", "by_name", "records"}
_resolve_cache: dict[str, "InstrumentRecord | None"] = {}
_build_lock = threading.Lock()      # the CLI warms the index from a background thread


@dataclass(frozen=True)
class InstrumentRecord:
    trading_symbol: str
    exchange: str
    name: str
    series: str
    isin: str
    lot_size: int
    buy_allowed: bool
    sell_allowed: bool

    @property
    def instrument(self) -> Instrument:
        return Instrument(self.trading_symbol, self.exchange)

    def to_dict(self) -> dict:
        return {
            "trading_symbol": self.trading_symbol,
            "exchange": self.exchange,
            "name": self.name,
            "isin": self.isin or None,
            "lot_size": self.lot_size,
            "tradable": self.buy_allowed and self.sell_allowed,
        }


# --------------------------------------------------------------------------- #
# loading / caching
# --------------------------------------------------------------------------- #
def _norm_name(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\b(ltd|limited|the|of|india)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _download() -> bytes | None:
    try:
        log.info("Downloading Groww instrument master (first run / daily refresh)…")
        resp = requests.get(CSV_URL, timeout=30)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:  # noqa: BLE001 - never fatal; we degrade gracefully
        log.warning("Instrument master download failed: %s", exc)
        return None


def _load_csv_text() -> str | None:
    fresh = CACHE_FILE.exists() and (time.time() - CACHE_FILE.stat().st_mtime) < MAX_AGE_SECONDS
    if fresh:
        try:
            return CACHE_FILE.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    data = _download()
    if data is not None:
        try:
            CACHE_FILE.write_bytes(data)
        except OSError as exc:
            log.debug("Could not cache instrument master: %s", exc)
        return data.decode("utf-8", errors="replace")

    # Download failed — fall back to a stale cache if we have one.
    if CACHE_FILE.exists():
        log.warning("Using stale instrument master cache (download failed).")
        try:
            return CACHE_FILE.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    return None


def _build() -> None:
    global _index
    idx: dict = {"by_symbol": {}, "by_name": {}, "records": []}
    text = _load_csv_text()
    if not text:
        log.warning("No instrument master available; symbol resolution will fall back "
                    "to raw normalization (less reliable).")
        _index = idx
        return

    count = 0
    for row in csv.DictReader(io.StringIO(text)):
        if (row.get("segment") or "").upper() != "CASH":
            continue
        exch = (row.get("exchange") or "").upper()
        if exch not in (NSE, BSE):
            continue
        ts = (row.get("trading_symbol") or "").upper().strip()
        if not ts:
            continue
        try:
            lot = int(float(row.get("lot_size") or 1))
        except (TypeError, ValueError):
            lot = 1
        rec = InstrumentRecord(
            trading_symbol=ts,
            exchange=exch,
            name=(row.get("name") or ts).strip(),
            series=(row.get("series") or "").upper(),
            isin=(row.get("isin") or "").strip(),
            lot_size=lot,
            buy_allowed=str(row.get("buy_allowed", "")).strip() in ("1", "true", "True"),
            sell_allowed=str(row.get("sell_allowed", "")).strip() in ("1", "true", "True"),
        )
        idx["records"].append(rec)
        idx["by_symbol"].setdefault(ts, []).append(rec)
        idx["by_name"].setdefault(_norm_name(rec.name), []).append(rec)
        count += 1

    log.info("Instrument master loaded: %d cash-segment instruments.", count)
    _index = idx


def ensure_loaded() -> bool:
    """Build the index if needed (thread-safe). Returns True if any instruments
    are available."""
    global _index
    if _index is None:
        with _build_lock:
            if _index is None:
                _build()
    return bool(_index and _index["records"])


def available() -> bool:
    return bool(_index and _index["records"])


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
def _exchange_bonus(rec: InstrumentRecord, exchange: str | None) -> int:
    if exchange and rec.exchange == exchange.upper():
        return 8
    if rec.exchange == NSE:          # NSE is the primary listing by default
        return 6
    return 0


def _series_bonus(rec: InstrumentRecord) -> int:
    return 3 if rec.series == "EQ" else 0  # prefer the main equity series


_ETF_HINTS = ("etf", "bees", "ietf")


def _etf_penalty(rec: InstrumentRecord, query_lower: str) -> int:
    """Demote ETFs/index funds when the user clearly asked for a company stock.
    (ETFs share segment=CASH and series=EQ, so name length alone would otherwise
    let a short ETF name like 'ICICIFIN' beat 'ICICI Bank'.)"""
    if any(h in query_lower for h in _ETF_HINTS):
        return 0  # user explicitly wants an ETF
    blob = f"{rec.trading_symbol} {rec.name}".lower()
    return 200 if any(h in blob for h in _ETF_HINTS) else 0


def _structured_penalty(rec: InstrumentRecord, query_upper: str) -> int:
    """Small tie-break nudge: ETFs/structured products often carry digits in the
    ticker (ICICIB22, CPSEETF). When the user typed no digits, lightly prefer the
    plain-letter stock ticker. Too small to override an exact match."""
    if any(c.isdigit() for c in query_upper):
        return 0
    return 15 if any(c.isdigit() for c in rec.trading_symbol) else 0


def search(query: str, limit: int = 8, exchange: str | None = None) -> list[InstrumentRecord]:
    """Return ranked instrument matches for a name or ticker (best first)."""
    if not ensure_loaded():
        return []
    idx = _index
    q = (query or "").strip()
    if not q:
        return []

    qsym = q.upper()
    # Strip a leading exchange prefix ("NSE:RELIANCE", "NSE_INFY") and adopt it as
    # the exchange filter so LLM/TradingView-style symbols resolve to the master.
    pm = re.match(r"^(NSE|BSE)[\s:_\-]+(.+)$", qsym)
    if pm:
        exchange = exchange or pm.group(1)
        qsym = pm.group(2).strip()
        q = qsym
    for suf in (".NS", ".BO"):
        if qsym.endswith(suf):
            qsym = qsym[:-3]
    qname = _norm_name(q)
    qlower = q.lower()

    best: dict[str, tuple[int, InstrumentRecord]] = {}

    def add(rec: InstrumentRecord, base: int) -> None:
        score = (
            base
            + _exchange_bonus(rec, exchange)
            + _series_bonus(rec)
            - _etf_penalty(rec, qlower)
            - _structured_penalty(rec, qsym)
        )
        key = f"{rec.trading_symbol}|{rec.exchange}"
        if key not in best or score > best[key][0]:
            best[key] = (score, rec)

    # 1. exact ticker
    for rec in idx["by_symbol"].get(qsym, []):
        add(rec, 1000)
    # 2. exact (normalised) company name
    for rec in idx["by_name"].get(qname, []):
        add(rec, 950)
    # 3. ticker prefix (e.g. "icici" -> ICICIBANK)
    if len(qsym) >= 2:
        for sym, recs in idx["by_symbol"].items():
            if sym != qsym and sym.startswith(qsym):
                for rec in recs:
                    add(rec, 780 - len(sym))
    # 4. name prefix / substring / token match
    if qname:
        qtokens = set(qname.split())
        for nm, recs in idx["by_name"].items():
            if nm == qname:
                continue
            if nm.startswith(qname):
                base = 820 - len(nm)
            elif qname in nm:
                base = 680 - len(nm)
            elif qtokens and qtokens.issubset(set(nm.split())):
                base = 560 - len(nm)
            else:
                continue
            for rec in recs:
                add(rec, base)

    ranked = sorted(best.values(), key=lambda sr: (-sr[0], len(sr[1].trading_symbol), sr[1].name))
    return [rec for _, rec in ranked][:limit]


def resolve(query: str, exchange: str | None = None) -> InstrumentRecord | None:
    """Resolve a name/ticker to the single best Groww instrument, or None."""
    cache_key = f"{(query or '').strip().lower()}|{(exchange or '').upper()}"
    if cache_key in _resolve_cache:
        return _resolve_cache[cache_key]
    matches = search(query, limit=1, exchange=exchange)
    rec = matches[0] if matches else None
    _resolve_cache[cache_key] = rec
    return rec


def to_instrument(query: str, exchange: str | None = None) -> Instrument:
    """Drop-in replacement for symbols.normalize() that first consults the Groww
    master. Falls back to raw normalization when the master is unavailable or has
    no match (so behaviour degrades gracefully, never crashes)."""
    rec = resolve(query, exchange)
    if rec is not None:
        return rec.instrument
    return normalize(query, exchange)
