"""Preflight — check the whole pipeline before trusting it with money.

    python -m trinetra_mcp.preflight

Run it after every deploy and before switching anyone to live. It exercises the
real pipeline rather than asserting configuration exists: the vault actually
seals and opens a value, the database actually answers a query, the schema is
actually current, the tool surface is actually complete.

Exit code is 0 only if nothing FAILED. Warnings do not fail the run — they are
things that are missing on purpose in some deployments (a local server has no
database, and that is correct).

Checks that would place an order are deliberately absent. Nothing here trades.
"""

from __future__ import annotations

import asyncio
import os
import contextlib
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""
    took_ms: int = 0


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", took_ms: int = 0) -> None:
        self.results.append(Result(name, status, detail, took_ms))

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if r.status == FAIL]

    @property
    def warned(self) -> list[Result]:
        return [r for r in self.results if r.status == WARN]


def _check(report: Report, name: str, fn: Callable[[], tuple[str, str]]) -> None:
    """Run one check, timing it and turning any exception into a FAIL."""
    started = time.monotonic()
    try:
        status, detail = fn()
    except Exception as exc:  # noqa: BLE001 - a crashing check is a failing check
        status, detail = FAIL, f"{type(exc).__name__}: {exc}"
    report.add(name, status, detail, int((time.monotonic() - started) * 1000))


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def _check_mode() -> tuple[str, str]:
    from trinetra import store

    hosted = store.database_url() is not None
    transport = os.getenv("TRINETRA_TRANSPORT", "stdio")
    return PASS, f"{'hosted' if hosted else 'local'} / {transport} transport"


def _check_vault() -> tuple[str, str]:
    """Seal and open a real value — configuration alone proves nothing."""
    from trinetra import store, vault

    if not vault.is_configured():
        if store.database_url() is not None:
            return FAIL, "TRINETRA_MASTER_KEY is not set, so broker linking is dead"
        return WARN, "no master key (fine for a local paper-only server)"

    probe = {"probe": "trinetra-preflight"}
    ciphertext, key_id = vault.encrypt(probe)
    if vault.decrypt(ciphertext, key_id).reveal() != probe:
        return FAIL, "round trip returned different data"
    if b"trinetra-preflight" in ciphertext:
        return FAIL, "plaintext is visible in the ciphertext"
    keys = sorted(vault.master_keys())
    return PASS, f"seal/open verified; key(s) {keys}, active {vault.active_key_id()}"


def _check_auth() -> tuple[str, str]:
    from trinetra import store
    from trinetra_mcp.auth import auth_enabled

    if auth_enabled():
        return PASS, f"issuer {os.getenv('OAUTH_ISSUER')}"
    if store.database_url() is not None:
        return FAIL, "database configured but OAuth is not — the server will refuse to start"
    return WARN, "no OAuth (fine for a local single-user server)"


def _check_database() -> tuple[str, str]:
    from sqlalchemy import inspect, text

    from trinetra import db, store

    if store.database_url() is None:
        return WARN, "no DATABASE_URL — using the local file store"

    with db.session_scope() as session:
        session.execute(text("SELECT 1"))
    present = set(inspect(db.get_engine()).get_table_names())
    expected = set(db.Base.metadata.tables)
    missing = expected - present
    if missing:
        return FAIL, f"tables missing (run `alembic upgrade head`): {sorted(missing)}"
    return PASS, f"reachable, {len(expected)} tables present"


def _check_migrations() -> tuple[str, str]:
    """The schema must be at head — a stale schema fails at the worst moment."""
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from trinetra import db, store

    if store.database_url() is None:
        return WARN, "not applicable without a database"

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    head = script.get_current_head()
    with db.get_engine().connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    if current != head:
        return FAIL, f"schema at {current or 'nothing'}, expected {head}"
    return PASS, f"at head ({head})"


def _check_market_data() -> tuple[str, str]:
    """Live prices are the input to every cap and every fill."""
    from trinetra.services import research

    quote = research.get_quote("RELIANCE")
    price = quote.get("last_price")
    if not price:
        return FAIL, f"no price returned: {quote.get('error', quote)}"
    return PASS, f"RELIANCE {price} via {quote.get('source')}"


def _check_instruments() -> tuple[str, str]:
    """Symbol resolution is what stops a wrong ticker reaching a broker."""
    from trinetra import instruments

    if not instruments.ensure_loaded():
        return WARN, "instrument master unavailable — symbol resolution degrades"
    record = instruments.resolve("infosys")
    if record is None or record.trading_symbol != "INFY":
        return FAIL, f"'infosys' resolved to {record and record.trading_symbol!r}, expected INFY"
    return PASS, f"loaded; 'infosys' -> {record.trading_symbol}@{record.exchange}"


def _check_broker_sdks() -> tuple[str, str]:
    """A hosted server without these cannot place a single real order.

    Only a warning locally, where paper trading is the whole point — but a hard
    failure on a server that offers live trading, because the gap is otherwise
    invisible until a user's first real order.
    """
    from trinetra import store

    available, missing = [], []
    for label, module in (("groww", "growwapi"), ("zerodha", "kiteconnect"),
                          ("groww-totp", "pyotp")):
        try:
            __import__(module)
            available.append(label)
        except ImportError:
            missing.append(label)
    if missing:
        if store.database_url() is not None:
            return FAIL, f"live trading is dead — not installed: {missing}"
        return WARN, f"available: {available}; not installed: {missing} (paper still works)"
    return PASS, f"all broker SDKs importable ({', '.join(available)})"


EXPECTED_TOOLS = {
    "setup_account", "get_account_status", "lookup_stocks", "get_live_quote",
    "get_stock_details", "analyze_stock", "view_portfolio", "get_funds",
    "get_order_history", "get_performance", "place_order", "confirm_order",
    "list_brokers", "link_broker", "get_broker_status", "unlink_broker",
    "switch_trading_mode", "set_kill_switch", "set_daily_limits",
}
CREDENTIAL_WORDS = ("api_key", "apikey", "secret", "password", "totp", "access_token")


def _check_tool_surface() -> tuple[str, str]:
    from trinetra_mcp.server import build_server

    tools = asyncio.run(build_server().list_tools())
    names = {t.name for t in tools}
    missing = EXPECTED_TOOLS - names
    if missing:
        return FAIL, f"tools missing: {sorted(missing)}"

    # Structural guarantee: no tool may collect a broker credential.
    offenders = [
        f"{t.name}.{p}" for t in tools
        for p in (t.inputSchema or {}).get("properties", {})
        if any(word in p.lower() for word in CREDENTIAL_WORDS)
    ]
    if offenders:
        return FAIL, f"tools accept credential-shaped inputs: {offenders}"
    return PASS, f"{len(names)} tools, none accepting credentials"


def _check_order_safety() -> tuple[str, str]:
    """The cap must fail closed when an order's value cannot be determined."""
    from trinetra.broker.base import BrokerError, OrderRequest
    from trinetra.broker.paper_broker import PaperBroker

    broker = PaperBroker()
    request = OrderRequest(trading_symbol="RELIANCE", transaction_type="buy", quantity=1)
    try:
        broker.guard_order(request.normalised(), reference_price=None)
    except BrokerError:
        pass  # correct: unknown value is refused
    else:
        return FAIL, "an order with no determinable value was allowed through"

    cap = broker.ctx.max_order_value
    try:
        oversized = OrderRequest(
            trading_symbol="RELIANCE", transaction_type="buy",
            quantity=int(cap // 100) + 1000,
        )
        broker.guard_order(oversized.normalised(), reference_price=100.0)
    except BrokerError:
        return PASS, f"fails closed on unknown value; cap ₹{cap:,.0f} enforced"
    return FAIL, "an order above the per-order cap was allowed through"


def _check_charts() -> tuple[str, str]:
    from trinetra import charts

    png = charts.portfolio_chart({
        "holdings": [
            {"trading_symbol": "A", "quantity": 1, "current_value": 100.0, "pnl": 5.0},
            {"trading_symbol": "B", "quantity": 1, "current_value": 50.0, "pnl": -2.0},
        ],
        "summary": {"current_value": 150.0, "holdings_count": 2, "overall_pnl": 3.0},
    })
    if not png.startswith(b"\x89PNG"):
        return FAIL, "renderer did not produce a PNG"
    return PASS, f"rendered {len(png) // 1024} KB PNG"


def _check_live_readiness() -> tuple[str, str]:
    from trinetra import limits

    if limits.global_live_disabled():
        return WARN, f"{limits.GLOBAL_KILL_ENV} is set — live trading is off for everyone"
    return PASS, "global kill switch is off (live trading permitted)"


CHECKS: list[tuple[str, Callable[[], tuple[str, str]]]] = [
    ("deployment mode", _check_mode),
    ("credential vault", _check_vault),
    ("authentication", _check_auth),
    ("database", _check_database),
    ("schema migrations", _check_migrations),
    ("market data", _check_market_data),
    ("instrument master", _check_instruments),
    ("broker SDKs", _check_broker_sdks),
    ("tool surface", _check_tool_surface),
    ("order safety cap", _check_order_safety),
    ("chart rendering", _check_charts),
    ("live kill switch", _check_live_readiness),
]


def run() -> Report:
    report = Report()
    for name, fn in CHECKS:
        _check(report, name, fn)
    return report


def main() -> int:
    with contextlib.suppress(AttributeError, ValueError):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

    print("Trinetra preflight\n" + "─" * 62)
    report = run()
    marks = {PASS: "  ok  ", WARN: " warn ", FAIL: " FAIL "}
    for result in report.results:
        print(f"[{marks[result.status]}] {result.name:<20} {result.detail}"
              f"{'' if result.took_ms < 200 else f'  ({result.took_ms} ms)'}")

    print("─" * 62)
    if report.failed:
        print(f"{len(report.failed)} check(s) FAILED — do not go live:")
        for result in report.failed:
            print(f"  · {result.name}: {result.detail}")
        return 1
    if report.warned:
        print(f"All checks passed, with {len(report.warned)} warning(s).")
    else:
        print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
