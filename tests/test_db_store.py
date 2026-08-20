"""The database store: multi-tenant isolation, dedupe, and the audit chain.

Runs on SQLite for speed. The schema is written to be portable (JSONB is a
Postgres-only variant of a plain JSON column), so these exercise real behaviour;
Postgres-specific concerns like the append-only grant are deployment config, not
application logic.
"""
from __future__ import annotations

import subprocess
import sys
from itertools import pairwise

import pytest
from sqlalchemy import select

from trinetra import db
from trinetra.config import TradingMode
from trinetra.session import context_for_account


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    """A fresh database with the schema created, plus two isolated accounts."""
    url = f"sqlite:///{(tmp_path / 'trinetra.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    db.reset_engine()
    db.Base.metadata.create_all(db.get_engine())
    yield url
    db.reset_engine()


def _ctx(account_id: str, user_id: str = "u"):
    return context_for_account(
        user_id=user_id, account_id=account_id,
        trading_mode=TradingMode.PAPER, max_order_value=100_000.0,
    )


def _trade(ref: str, symbol: str = "RELIANCE", shares: int = 10, price: float = 2500.0):
    return {
        "timestamp": "2026-08-21T10:00:00", "symbol": symbol, "exchange": "NSE",
        "action": "buy", "product": "CNC", "shares": shares, "price": price,
        "total": shares * price, "order_id": f"PAPER-{ref}", "reference_id": ref,
    }


# --------------------------------------------------------------------------- #
# provisioning
# --------------------------------------------------------------------------- #
def test_first_login_provisions_user_and_account(sqlite_db):
    user_id, account_id = db.upsert_user("auth0|abc", "a@example.com")
    assert user_id and account_id

    # Same subject again is the same user and account, not a duplicate.
    again = db.upsert_user("auth0|abc", "a@example.com")
    assert again == (user_id, account_id)

    account = db.load_account_row(account_id)
    assert account.mode == "paper"
    assert account.live_activated_at is None, "live must never be on by default"
    assert account.kill_switch is False


def test_different_subjects_get_different_accounts(sqlite_db):
    _, account_a = db.upsert_user("auth0|a")
    _, account_b = db.upsert_user("auth0|b")
    assert account_a != account_b


# --------------------------------------------------------------------------- #
# trades
# --------------------------------------------------------------------------- #
def test_trades_round_trip_and_dedupe(sqlite_db):
    _, account_id = db.upsert_user("auth0|a")
    store = db.DbStore(_ctx(account_id))

    store.save_trades([_trade("r1")])
    store.save_trades([_trade("r1"), _trade("r2")])  # broker resends the whole log
    trades = store.load_trades()

    assert [t["reference_id"] for t in trades] == ["r1", "r2"], "r1 must not double-insert"
    assert trades[0]["shares"] == 10
    assert trades[0]["total"] == 25000.0


def test_accounts_cannot_see_each_others_trades(sqlite_db):
    _, account_a = db.upsert_user("auth0|a")
    _, account_b = db.upsert_user("auth0|b")

    db.DbStore(_ctx(account_a, "a")).save_trades([_trade("r1", "RELIANCE")])
    db.DbStore(_ctx(account_b, "b")).save_trades([_trade("r2", "TCS")])

    assert [t["symbol"] for t in db.DbStore(_ctx(account_a, "a")).load_trades()] == ["RELIANCE"]
    assert [t["symbol"] for t in db.DbStore(_ctx(account_b, "b")).load_trades()] == ["TCS"]


def test_store_requires_an_account(sqlite_db):
    from trinetra.session import context_for_user

    with pytest.raises(RuntimeError, match="account_id"):
        db.DbStore(context_for_user("nobody"))


# --------------------------------------------------------------------------- #
# audit chain
# --------------------------------------------------------------------------- #
def test_audit_events_are_hash_chained(sqlite_db):
    _, account_id = db.upsert_user("auth0|a")
    store = db.DbStore(_ctx(account_id))

    store.append_audit("preview", {"symbol": "RELIANCE"})
    store.append_audit("submit", {"symbol": "RELIANCE"})
    store.append_audit("filled", {"symbol": "RELIANCE"})

    with db.session_scope() as session:
        rows = session.scalars(
            select(db.AuditEvent).order_by(db.AuditEvent.ts)
        ).all()

    assert [r.event for r in rows] == ["preview", "submit", "filled"]
    assert rows[0].prev_hash is None
    for earlier, later in pairwise(rows):
        assert later.prev_hash == earlier.row_hash, "each row must commit to the previous"


def test_tampering_with_an_audit_row_breaks_the_chain(sqlite_db):
    _, account_id = db.upsert_user("auth0|a")
    store = db.DbStore(_ctx(account_id))
    store.append_audit("submit", {"quantity": 1})

    with db.session_scope() as session:
        row = session.scalars(select(db.AuditEvent)).one()
        original_hash = row.row_hash
        body = {"account_id": row.account_id, "user_id": row.user_id, "event": row.event,
                "mode": row.mode, "payload": {"quantity": 9999}}  # someone edits the qty

    assert db.chain_hash(None, body) != original_hash


def test_audit_failure_never_raises(sqlite_db, monkeypatch):
    """A trade the user authorised must not fail because auditing broke."""
    _, account_id = db.upsert_user("auth0|a")
    store = db.DbStore(_ctx(account_id))

    def boom():
        raise RuntimeError("database down")

    monkeypatch.setattr(db, "session_scope", boom)
    store.append_audit("submit", {"symbol": "X"})  # must not raise


# --------------------------------------------------------------------------- #
# paper broker over the database
# --------------------------------------------------------------------------- #
def test_paper_broker_works_against_the_database(sqlite_db, monkeypatch):
    import trinetra.market_data as market_data
    from trinetra.broker.base import OrderRequest
    from trinetra.broker.paper_broker import PaperBroker

    monkeypatch.setattr(market_data, "ltp_many", lambda syms: dict.fromkeys(syms, 2600.0))
    _, account_id = db.upsert_user("auth0|a")
    broker = PaperBroker(_ctx(account_id))

    result = broker.place_order(
        OrderRequest(trading_symbol="RELIANCE", transaction_type="buy", quantity=4),
        reference_price=2500.0,
    )
    assert result.status == "filled"

    holdings = broker.get_holdings()
    assert len(holdings) == 1
    assert holdings[0].trading_symbol == "RELIANCE"
    assert holdings[0].quantity == 4
    assert broker.get_funds().available_cash == pytest.approx(100_000 - 10_000)


# --------------------------------------------------------------------------- #
# migrations
# --------------------------------------------------------------------------- #
def test_migrations_run_clean_from_empty(tmp_path):
    """Alembic must build the schema from nothing — not just metadata.create_all."""
    target = tmp_path / "migrated.db"
    env = {**__import__("os").environ, "DATABASE_URL": f"sqlite:///{target.as_posix()}"}
    done = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True, text=True, env=env, cwd=str(__import__("pathlib").Path(__file__).parent.parent),
    )
    assert done.returncode == 0, done.stderr
    assert target.exists()
