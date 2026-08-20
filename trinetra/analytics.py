"""Portfolio analytics — pure, deterministic maths over the paper trade log.

Everything here is a pure function of the trade-log list (plus, for a couple of
view helpers, a symbol→price map). No network, no broker, no I/O — so it is cheap
and exhaustively unit-testable, and it is the SINGLE source of truth for how the
paper portfolio derives cost basis and realized (booked) P&L.

Realized P&L uses the **average-cost method**: each buy blends into the running
average cost; each sell books `(sell_price − avg_cost) × shares` and retires that
cost basis proportionally, leaving the average of the remaining shares unchanged.
This matches how `PaperBroker` reports `average_price`, so unrealized and realized
P&L are always consistent with each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any

from trinetra.symbols import normalize


@dataclass
class RealizedEvent:
    """One booking of profit/loss: a sell (or the sold portion of one)."""
    symbol: str
    shares: float
    sell_price: float
    avg_cost: float
    realized: float          # (sell_price − avg_cost) × shares, in ₹
    realized_pct: float | None
    timestamp: str


@dataclass
class SymbolState:
    """Running per-symbol state after replaying the whole log."""
    symbol: str
    qty: float = 0.0                 # shares still held
    buy_qty: float = 0.0             # open shares carrying cost basis
    buy_cost: float = 0.0            # cost basis of the open shares
    realized: float = 0.0            # booked P&L to date on this symbol
    first_buy_ts: str | None = None  # for holding-period
    last_trade_ts: str | None = None

    @property
    def avg_cost(self) -> float:
        return self.buy_cost / self.buy_qty if self.buy_qty else 0.0


def parse_ts(ts: Any) -> datetime | None:
    """Parse an ISO timestamp from the trade log; None if unparseable."""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        try:
            return datetime.fromisoformat(ts.split(".")[0])
        except ValueError:
            return None


_to_dt = parse_ts  # internal alias


@dataclass
class Analysis:
    """The full result of replaying a trade log."""
    per_symbol: dict[str, SymbolState] = field(default_factory=dict)
    events: list[RealizedEvent] = field(default_factory=list)

    # --- realized P&L ---
    @property
    def total_realized(self) -> float:
        return round(sum(e.realized for e in self.events), 2)

    def realized_by_symbol(self) -> dict[str, float]:
        return {s.symbol: round(s.realized, 2) for s in self.per_symbol.values()
                if abs(s.realized) > 1e-9}

    # --- trade statistics ---
    def stats(self) -> dict[str, Any]:
        wins = [e.realized for e in self.events if e.realized > 0]
        losses = [e.realized for e in self.events if e.realized < 0]
        n = len(self.events)
        gross_profit = round(sum(wins), 2)
        gross_loss = round(sum(losses), 2)
        return {
            "closed_trades": n,
            "winners": len(wins),
            "losers": len(losses),
            "win_rate": round(len(wins) / n * 100, 1) if n else None,
            "gross_profit": gross_profit,       # total booked GAINS (≥0)
            "gross_loss": gross_loss,           # total booked LOSSES (≤0)
            "net_realized": round(gross_profit + gross_loss, 2),
            "avg_win": round(gross_profit / len(wins), 2) if wins else None,
            "avg_loss": round(gross_loss / len(losses), 2) if losses else None,
        }

    def best_trade(self) -> RealizedEvent | None:
        return max(self.events, key=lambda e: e.realized, default=None)

    def worst_trade(self) -> RealizedEvent | None:
        return min(self.events, key=lambda e: e.realized, default=None)

    def closed_positions(self) -> list[dict[str, Any]]:
        """Symbols that were bought and are now fully exited (qty≈0), each with
        the total P&L booked over its life. Most-recently-closed first."""
        out = []
        for s in self.per_symbol.values():
            if round(s.qty) <= 0 and s.first_buy_ts is not None:
                out.append({
                    "symbol": s.symbol,
                    "realized": round(s.realized, 2),
                    "opened": s.first_buy_ts,
                    "closed": s.last_trade_ts,
                })
        out.sort(key=lambda d: d.get("closed") or "", reverse=True)
        return out

    def realized_today(self, today: date | None = None) -> float:
        """P&L booked from sells executed today (caller passes the date so this
        stays pure/deterministic)."""
        if today is None:
            return 0.0
        total = 0.0
        for e in self.events:
            dt = _to_dt(e.timestamp)
            if dt and dt.date() == today:
                total += e.realized
        return round(total, 2)


def analyze(trades: list[dict[str, Any]]) -> Analysis:
    """Replay the trade log chronologically and return the full analysis.

    `trades` is the on-disk paper log (oldest→newest). Symbols are normalised so
    'RELIANCE', 'RELIANCE.NS' and 'NSE_RELIANCE' collapse to one position.
    """
    result = Analysis()
    per = result.per_symbol
    for t in trades:
        raw_sym = t.get("symbol")
        if not raw_sym:
            continue
        sym = normalize(raw_sym).trading_symbol
        shares = float(t.get("shares", 0) or 0)
        price = float(t.get("price", 0) or 0)
        action = str(t.get("action", "")).lower()
        ts = t.get("timestamp")
        if shares <= 0:
            continue

        st = per.get(sym)
        if st is None:
            st = per[sym] = SymbolState(symbol=sym)
        st.last_trade_ts = ts if isinstance(ts, str) else st.last_trade_ts

        if action == "buy":
            if st.first_buy_ts is None and isinstance(ts, str):
                st.first_buy_ts = ts
            st.qty += shares
            st.buy_qty += shares
            st.buy_cost += shares * price
        elif action == "sell":
            if st.buy_qty > 0:
                avg = st.buy_cost / st.buy_qty
                retired = min(shares, st.buy_qty)
                realized = (price - avg) * retired
                st.buy_qty -= retired
                st.buy_cost -= retired * avg
                st.realized += realized
                result.events.append(RealizedEvent(
                    symbol=sym, shares=retired, sell_price=round(price, 2),
                    avg_cost=round(avg, 2), realized=round(realized, 2),
                    realized_pct=round((price - avg) / avg * 100, 2) if avg else None,
                    timestamp=ts if isinstance(ts, str) else "",
                ))
            st.qty -= shares
    return result
