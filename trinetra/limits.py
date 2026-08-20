"""Live-trading gates: activation, daily limits, and the kill switch.

Linking a broker does **not** enable real orders. Live trading is a separate,
explicit opt-in, and even then every order passes these checks.

Enforced in the service layer rather than in a tool, so no tool — and no host AI
calling one — can route around them. Each gate is independent: failing any one
rejects the order.

    activation    live must have been switched on deliberately
    kill switch   the user (or an operator) can stop live trading instantly
    per-order     the rupee ceiling already enforced by the broker layer
    daily value   total notional placed today
    daily count   number of orders placed today
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from trinetra.logging_setup import get_logger
from trinetra.session import SessionContext

log = get_logger(__name__)

GLOBAL_KILL_ENV = "TRINETRA_DISABLE_LIVE"


class LimitExceeded(RuntimeError):
    """A live order blocked by a gate. The message is shown to the user."""


@dataclass(frozen=True)
class DailyUsage:
    notional: float
    orders: int


def global_live_disabled() -> bool:
    """Operator-level switch: stops live trading for everyone, no deploy needed."""
    return os.getenv(GLOBAL_KILL_ENV, "").strip().lower() in ("1", "true", "yes", "on")


# NSE/BSE trade on Indian time, so "today" means the IST day. Bucketing by UTC
# would reset the limit at 05:30 IST — mid-morning — and split one trading
# session across two days.
IST = timezone(timedelta(hours=5, minutes=30))


def _start_of_trading_day() -> datetime:
    """Midnight IST for the current trading day, expressed in UTC."""
    today_ist = datetime.now(IST).date()
    return datetime.combine(today_ist, time.min, tzinfo=IST).astimezone(timezone.utc)


def daily_usage(ctx: SessionContext) -> DailyUsage:
    """Notional and count of live orders placed today by this account."""
    from trinetra import db

    if not ctx.account_id:
        return DailyUsage(0.0, 0)
    with db.session_scope() as session:
        since = db.as_column_datetime(session, _start_of_trading_day())
        row = session.execute(
            select(
                func.coalesce(func.sum(db.Order.estimated_value), 0.0),
                func.count(db.Order.id),
            ).where(
                (db.Order.account_id == ctx.account_id)
                & (db.Order.created_at >= since)
                # "rejected" is the only status we KNOW never reached the market.
                # Reservations ("submitting") and unresolved outcomes ("unknown")
                # both still count, so a failure cannot quietly free up budget.
                & (db.Order.status != "rejected")
            )
        ).one()
    return DailyUsage(float(row[0] or 0.0), int(row[1] or 0))


def account_limits(ctx: SessionContext) -> dict[str, Any]:
    """The limits in force, for display. Safe to show the user."""
    from trinetra import db

    row = db.load_account_row(ctx.account_id) if ctx.account_id else None
    usage = daily_usage(ctx)
    return {
        "live_activated": bool(row and row.live_activated_at),
        "kill_switch": bool(row and row.kill_switch) or global_live_disabled(),
        "per_order_cap": ctx.max_order_value,
        "daily_notional_cap": getattr(row, "daily_notional_cap", None),
        "daily_order_cap": getattr(row, "daily_order_cap", None),
        "used_today": {"notional": usage.notional, "orders": usage.orders},
    }


def check_live_order(ctx: SessionContext, estimated_value: float | None) -> None:
    """Every gate that stands between a live order and the broker.

    Raises LimitExceeded with a message meant for the user. Paper orders skip
    this entirely — there is nothing to protect.
    """
    if not ctx.is_live:
        return

    from trinetra import db

    if global_live_disabled():
        raise LimitExceeded(
            "Live trading is currently disabled on this server. Paper trading is "
            "unaffected."
        )

    row = db.load_account_row(ctx.account_id) if ctx.account_id else None
    if row is None:
        raise LimitExceeded("This account could not be loaded, so the order was refused.")

    if row.kill_switch:
        raise LimitExceeded(
            "Your kill switch is on, so live orders are blocked. Turn it off with "
            "set_kill_switch(enabled=False) when you want to trade again."
        )
    if row.live_activated_at is None:
        raise LimitExceeded(
            "Live trading is not activated on this account. Linking a broker is not "
            "enough — switch_trading_mode('live') turns it on deliberately."
        )

    # Fail closed: an order whose value we cannot determine cannot be checked
    # against a limit, so it does not get placed.
    if not estimated_value or estimated_value <= 0:
        raise LimitExceeded(
            "This order's value could not be determined, so the daily limits "
            "cannot be enforced. Refusing the order."
        )

    usage = daily_usage(ctx)
    if row.daily_notional_cap and usage.notional + estimated_value > row.daily_notional_cap:
        raise LimitExceeded(
            f"This order would take today's live trading to "
            f"₹{usage.notional + estimated_value:,.2f}, over your daily limit of "
            f"₹{row.daily_notional_cap:,.2f}. Already placed today: "
            f"₹{usage.notional:,.2f}."
        )
    if row.daily_order_cap and usage.orders + 1 > row.daily_order_cap:
        raise LimitExceeded(
            f"You have placed {usage.orders} live orders today, which is your "
            f"daily limit of {row.daily_order_cap}."
        )


# --------------------------------------------------------------------------- #
# switches
# --------------------------------------------------------------------------- #
def activate_live(ctx: SessionContext) -> dict[str, Any]:
    """Turn on real-money trading. Requires a linked broker."""
    from trinetra import brokerlink, db

    link = brokerlink.active_link(ctx)
    if link is None:
        raise LimitExceeded(
            "Link a broker before activating live trading — there is nothing to "
            "trade through yet."
        )

    with db.session_scope() as session:
        row = session.get(db.Account, ctx.account_id)
        if row is None:
            raise LimitExceeded("This account could not be loaded.")
        row.live_activated_at = datetime.now(timezone.utc)
        row.mode = "live"
        row.broker = link["broker"]
        row.kill_switch = False
        session.commit()

    log.warning("LIVE TRADING ACTIVATED for user %s via %s", ctx.user_id, link["broker"])
    return {"live_activated": True, "broker": link["broker"]}


def deactivate_live(ctx: SessionContext) -> dict[str, Any]:
    """Return the account to paper. Credentials stay linked."""
    from trinetra import db

    with db.session_scope() as session:
        row = session.get(db.Account, ctx.account_id)
        if row is None:
            raise LimitExceeded("This account could not be loaded.")
        row.live_activated_at = None
        row.mode = "paper"
        session.commit()
    log.info("Live trading deactivated for user %s", ctx.user_id)
    return {"live_activated": False, "mode": "paper"}


def set_kill_switch(ctx: SessionContext, enabled: bool) -> dict[str, Any]:
    """Stop or resume live trading immediately.

    Takes effect on the very next order — the flag is read from the database per
    order, not cached — and cached broker adapters are dropped so a revoked
    session cannot be reused.
    """
    from trinetra import db
    from trinetra.broker import registry

    with db.session_scope() as session:
        row = session.get(db.Account, ctx.account_id)
        if row is None:
            raise LimitExceeded("This account could not be loaded.")
        row.kill_switch = bool(enabled)
        session.commit()

    registry.forget(ctx)
    log.warning("Kill switch %s for user %s", "ON" if enabled else "OFF", ctx.user_id)
    return {"kill_switch": bool(enabled)}


def set_daily_caps(
    ctx: SessionContext,
    notional: float | None = None,
    orders: int | None = None,
) -> dict[str, Any]:
    """Tighten or relax this account's daily limits."""
    from trinetra import db

    with db.session_scope() as session:
        row = session.get(db.Account, ctx.account_id)
        if row is None:
            raise LimitExceeded("This account could not be loaded.")
        if notional is not None:
            row.daily_notional_cap = max(0.0, float(notional))
        if orders is not None:
            row.daily_order_cap = max(0, int(orders))
        session.commit()
    return account_limits(ctx)
