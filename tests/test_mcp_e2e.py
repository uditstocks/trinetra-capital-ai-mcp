"""End-to-end through the MCP tool surface — the real surface a host AI drives.

Fully offline: symbol resolution, prices and news are stubbed, and every test
gets its own throwaway data root so no user state leaks between cases.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from trinetra import broker as broker_pkg
from trinetra_mcp import runtime


def call(server, name, **kwargs):
    """Invoke an MCP tool and return its data payload.

    call_tool answers either (content, structured) or a bare content list, and a
    tool that also renders a chart returns [text, image] with no structured
    content — so identify the shape rather than unpacking blindly.
    """
    result = asyncio.run(server.call_tool(name, kwargs))
    if isinstance(result, tuple):
        content, structured = result
        if structured is not None:
            return structured
    else:
        content = result
    for block in content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"{name} returned no text block: {content!r}")


# --------------------------------------------------------------------------- #
# onboarding
# --------------------------------------------------------------------------- #
def test_account_tools_guide_user_before_setup(mcp):
    assert call(mcp, "get_account_status")["status"] == "not_set_up"
    portfolio = call(mcp, "view_portfolio")
    assert portfolio["status"] == "not_set_up"
    assert "setup_account" in portfolio["next_step"]


def test_setup_creates_then_reports_existing(mcp):
    created = call(mcp, "setup_account", mode="paper")
    assert created["status"] == "created"
    assert created["mode"] == "paper"
    assert created["starting_cash"] > 0

    again = call(mcp, "setup_account", mode="paper")
    assert again["status"] == "already_set_up"
    assert call(mcp, "get_account_status")["status"] == "ready"


def test_live_mode_refused_cleanly_not_as_error(mcp):
    out = call(mcp, "setup_account", mode="live")
    assert out["status"] == "unavailable"
    assert "next_step" in out
    # And it must not have quietly created a live account.
    assert call(mcp, "get_account_status")["status"] == "not_set_up"


def test_users_are_isolated(mcp, monkeypatch):
    call(mcp, "setup_account", mode="paper")
    call(mcp, "confirm_order",
         confirmation_token=call(mcp, "place_order", symbol="RELIANCE", action="buy",
                                 quantity=2)["confirmation_token"])
    assert call(mcp, "view_portfolio")["summary"]["holdings_count"] == 1

    monkeypatch.setenv("TRINETRA_USER_ID", "somebody_else")
    broker_pkg.reset_brokers()
    assert call(mcp, "get_account_status")["status"] == "not_set_up"


# --------------------------------------------------------------------------- #
# two-step orders
# --------------------------------------------------------------------------- #
def test_place_order_previews_without_trading(mcp):
    call(mcp, "setup_account", mode="paper")
    out = call(mcp, "place_order", symbol="RELIANCE", action="buy", quantity=10)

    assert out["status"] == "confirmation_required"
    assert out["preview"]["estimated_value"] == 25000.0
    assert out["confirmation_token"]
    # The account must be untouched until confirm_order runs.
    assert call(mcp, "view_portfolio")["summary"]["holdings_count"] == 0
    assert call(mcp, "get_order_history")["count"] == 0


def test_confirm_executes_the_previewed_order(mcp):
    call(mcp, "setup_account", mode="paper")
    preview = call(mcp, "place_order", symbol="RELIANCE", action="buy", quantity=10)
    result = call(mcp, "confirm_order",
                  confirmation_token=preview["confirmation_token"])

    assert result["status"] == "filled"
    assert result["quantity"] == 10
    assert result["estimated_value"] == 25000.0
    assert call(mcp, "view_portfolio")["summary"]["holdings_count"] == 1


def test_token_cannot_be_replayed(mcp):
    call(mcp, "setup_account", mode="paper")
    token = call(mcp, "place_order", symbol="RELIANCE", action="buy",
                 quantity=1)["confirmation_token"]
    assert call(mcp, "confirm_order", confirmation_token=token)["status"] == "filled"

    replay = call(mcp, "confirm_order", confirmation_token=token)
    assert replay["status"] == "rejected"
    # One fill only — the replay must not have bought a second share.
    assert call(mcp, "view_portfolio")["holdings"][0]["quantity"] == 1


def test_unknown_and_expired_tokens_are_rejected(mcp, monkeypatch):
    call(mcp, "setup_account", mode="paper")
    assert call(mcp, "confirm_order", confirmation_token="tcai_made_up")["status"] == "rejected"

    # Issue with a zero-second lifetime so the token is stale on arrival.
    monkeypatch.setattr(runtime, "TTL_SECONDS", 0.0)
    token = call(mcp, "place_order", symbol="RELIANCE", action="buy",
                 quantity=1)["confirmation_token"]
    expired = call(mcp, "confirm_order", confirmation_token=token)
    assert expired["status"] == "rejected"
    assert call(mcp, "view_portfolio")["summary"]["holdings_count"] == 0


def test_token_is_scoped_to_its_user(mcp, monkeypatch):
    call(mcp, "setup_account", mode="paper")
    token = call(mcp, "place_order", symbol="RELIANCE", action="buy",
                 quantity=1)["confirmation_token"]

    monkeypatch.setenv("TRINETRA_USER_ID", "attacker")
    broker_pkg.reset_brokers()
    call(mcp, "setup_account", mode="paper")
    stolen = call(mcp, "confirm_order", confirmation_token=token)
    assert stolen["status"] == "rejected"


def test_safety_cap_blocks_at_preview(mcp):
    call(mcp, "setup_account", mode="paper")
    out = call(mcp, "place_order", symbol="RELIANCE", action="buy", quantity=1000)
    assert out["status"] == "rejected"
    assert "safety cap" in out["error"]
    assert "confirmation_token" not in out


# --------------------------------------------------------------------------- #
# input guards
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    {"symbol": "RELIANCE", "action": "buy", "quantity": 0},
    {"symbol": "RELIANCE", "action": "buy", "quantity": -5},
    {"symbol": "RELIANCE", "action": "hodl", "quantity": 1},
    {"symbol": "RELIANCE", "action": "buy", "quantity": 1, "order_type": "yolo"},
    {"symbol": "RELIANCE", "action": "buy", "quantity": 1, "price": -10},
])
def test_bad_arguments_are_rejected_not_raised(mcp, bad):
    call(mcp, "setup_account", mode="paper")
    out = call(mcp, "place_order", **bad)
    assert out["status"] == "rejected"
    assert out["error"]
