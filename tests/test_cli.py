"""CLI turn extraction + fast-path intent matching — the hallucination fixes.

These pin the exact failures seen in the field:
- the supervisor saying "your portfolio has been displayed" while showing nothing,
- the CLI echoing the user's own message back after an approved order,
- an approved-but-cap-blocked order producing no visible result.
"""
from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from trinetra.cli import (
    _classify,
    _fast_intent,
    _final_ai_text,
    _is_help,
    _msg_text,
    _tool_payloads,
)


def _tool_msg(name: str, payload: dict) -> ToolMessage:
    return ToolMessage(content=json.dumps(payload), name=name, tool_call_id="tc1")


# --- payload extraction -------------------------------------------------------
def test_tool_payloads_extracts_json_dicts():
    msgs = [
        HumanMessage(content="show my portfolio"),
        _tool_msg("view_portfolio", {"mode": "paper", "holdings": [], "display": "x"}),
        AIMessage(content="done"),
    ]
    payloads = _tool_payloads(msgs)
    assert len(payloads) == 1
    assert payloads[0][0] == "view_portfolio"
    assert payloads[0][1]["mode"] == "paper"


def test_tool_payloads_ignores_non_json():
    msgs = [ToolMessage(content="Successfully transferred to trading_agent",
                        name="transfer_to_trading_agent", tool_call_id="tc2")]
    assert _tool_payloads(msgs) == []


# --- classification -----------------------------------------------------------
def test_classify_portfolio_orders_funds_order():
    assert _classify("view_portfolio", {"holdings": []}) == "portfolio"
    assert _classify("get_order_history", {"orders": []}) == "orders"
    assert _classify("get_funds", {"available_cash": 1, "mode": "paper"}) == "funds"
    assert _classify("place_order", {"status": "filled"}) == "order"
    assert _classify("cancel_order", {"status": "error"}) == "order"
    # name missing -> classify by structure
    assert _classify("", {"holdings": [1]}) == "portfolio"
    assert _classify("", {"status": "rejected", "order_id": "X"}) == "order"
    # research payloads are NOT deterministically rendered
    assert _classify("get_live_quote", {"last_price": 100}) is None


# --- final text selection -------------------------------------------------------
def test_final_text_prefers_specialist_over_supervisor():
    msgs = [
        HumanMessage(content="should i buy infy?"),
        AIMessage(content="INFY looks neutral. RSI 55.", name="sentiment_agent"),
        AIMessage(content="The analysis has been displayed."),  # supervisor rewrite
    ]
    assert _final_ai_text(msgs) == "INFY looks neutral. RSI 55."


def test_final_text_never_returns_human_message():
    msgs = [HumanMessage(content="buy 200 shares of infosys at market price")]
    assert _final_ai_text(msgs) == ""  # the old bug echoed this back


def test_final_text_falls_back_to_supervisor():
    msgs = [HumanMessage(content="hi"), AIMessage(content="Hello! How can I help?")]
    assert _final_ai_text(msgs) == "Hello! How can I help?"


def test_msg_text_handles_content_blocks():
    m = AIMessage(content=[{"type": "text", "text": "part one"},
                           {"type": "text", "text": "part two"}])
    assert _msg_text(m) == "part one part two"


# --- fast-path intent -----------------------------------------------------------
def test_fast_intent_portfolio_variants():
    for cmd in ("portfolio", "show my portfolio", "view holdings", "my portfolio please",
                "i cant see my portfolio", "show my pnl", "positions"):
        assert _fast_intent(cmd) == "portfolio", cmd


def test_fast_intent_funds_variants():
    for cmd in ("funds", "show my balance", "available cash", "check my funds"):
        assert _fast_intent(cmd) == "funds", cmd


def test_fast_intent_orders_variants():
    for cmd in ("orders", "order history", "show my orders", "order book", "my trades"):
        assert _fast_intent(cmd) == "orders", cmd


def test_fast_intent_never_swallows_actions():
    """Anything that could be an instruction MUST go to the agents."""
    for cmd in ("buy 200 shares of infosys", "sell my holdings", "sell everything",
                "cancel my order", "buy reliance", "what is the price of itc",
                "should i buy tcs", "portfolio of reliance", "trade",
                "exit my positions in itc", "should i book profit in tcs",
                "net position of reliance", "book my profit in infy",
                "what is my net worth of reliance"):
        assert _fast_intent(cmd) is None, cmd


def test_fast_intent_performance_variants():
    for cmd in ("performance", "show my performance", "stats", "my stats",
                "booked pnl", "realized pnl", "show booked", "win rate", "winrate",
                "how much profit have i booked", "am i net positive",
                "how much loss have i booked till now"):
        assert _fast_intent(cmd) == "performance", cmd


def test_fast_intent_help_variants():
    for cmd in ("help", "intro", "introduce yourself", "tell me about yourself",
                "what can you do", "who are you", "features", "your services",
                "about", "menu"):
        assert _fast_intent(cmd) == "help", cmd


def test_is_help_does_not_fire_on_stock_questions():
    """'tell me about <stock>' must reach the research agent, not the help screen."""
    for cmd in ("tell me about reliance", "about reliance", "what is the price",
                "about my portfolio value", "buy infy"):
        assert not _is_help(cmd), cmd


def test_pnl_still_routes_to_portfolio():
    # 'pnl' alone -> portfolio (holdings + unrealized P&L); 'booked pnl' -> performance.
    assert _fast_intent("pnl") == "portfolio"
    assert _fast_intent("show my p&l") == "portfolio"
    assert _fast_intent("booked pnl") == "performance"
