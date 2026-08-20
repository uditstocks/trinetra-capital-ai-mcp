"""Interactive command-line interface with the Human-in-the-Loop approval gate.

Run via `python main.py`. Design rules for a financial terminal:

1. DETERMINISTIC MONEY. Anything with a rupee sign on it is rendered straight
   from the broker/tool JSON payloads (ToolMessages), never from LLM prose. The
   supervisor cannot hallucinate "your portfolio has been displayed" — if a
   portfolio/order/funds payload exists in the turn, we render it ourselves.
2. FAST PATH. Pure read commands ("portfolio", "orders", "funds") skip the
   agents entirely — no LLM call, instant answer, zero hallucination surface.
3. LIVE SAFETY. A clear PAPER/LIVE banner, an explicit confirmation before a
   live session, and a rich approval panel before any order reaches a broker.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from trinetra import ui
from trinetra.config import settings
from trinetra.logging_setup import get_logger

log = get_logger(__name__)

# Headroom so the occasional extra supervisor->worker hop doesn't crash a turn.
RECURSION_LIMIT = 40

AGENT_NAMES = {"research_agent", "sentiment_agent", "trading_agent"}
ORDER_TOOLS = {"place_order", "cancel_order", "modify_order"}


# --------------------------------------------------------------------------- #
# turn-result extraction (pure functions — unit tested)
# --------------------------------------------------------------------------- #
def _msg_text(m) -> str:
    """Best-effort plain text of a message (handles content blocks)."""
    c = getattr(m, "content", "")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        parts = [p.get("text", "") if isinstance(p, dict) else str(p) for p in c]
        return " ".join(x for x in parts if x).strip()
    return ""


def _tool_payloads(messages) -> list[tuple[str, dict]]:
    """All JSON dict payloads returned by tools this turn, in order."""
    out: list[tuple[str, dict]] = []
    for m in messages:
        if not isinstance(m, ToolMessage):
            continue
        text = _msg_text(m)
        if not text.startswith("{"):
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            out.append((getattr(m, "name", "") or "", data))
    return out


def _classify(name: str, data: dict) -> str | None:
    """Which deterministic renderer (if any) owns this tool payload."""
    if name in ORDER_TOOLS:
        return "order"
    if name == "get_performance" or "total_realized" in data or "closed_positions" in data:
        return "performance"
    if name == "view_portfolio" or "holdings" in data:
        return "portfolio"
    if name == "get_order_history" or "orders" in data:
        return "orders"
    if name == "get_funds" or ("available_cash" in data and "mode" in data):
        return "funds"
    if name == "get_order_status" or ("status" in data and "order_id" in data):
        return "order"
    return None


def _final_ai_text(messages) -> str:
    """The most faithful final answer: prefer the specialist's own last words
    over the supervisor's relay (which weak models tend to rewrite)."""
    fallback = ""
    for m in reversed(messages):
        if not isinstance(m, AIMessage):
            continue
        text = _msg_text(m)
        if not text:
            continue
        if getattr(m, "name", None) in AGENT_NAMES:
            return text
        if not fallback:
            fallback = text
    return fallback


def _show_final(result) -> None:
    """Render one turn: deterministic payloads first, agent prose only when no
    money data is involved. Never prints a Human/Tool message as the answer."""
    messages = result.get("messages", [])
    payloads = _tool_payloads(messages)

    shown = False
    # Every order action result, chronologically — approvals must never vanish.
    for name, data in payloads:
        if _classify(name, data) == "order":
            ui.order_result_view(name or "order", data)
            shown = True

    # Portfolio / performance / orders / funds: the last payload of each kind.
    latest: dict[str, dict] = {}
    for name, data in payloads:
        kind = _classify(name, data)
        if kind in ("portfolio", "performance", "orders", "funds"):
            latest[kind] = data
    for kind, data in latest.items():
        {"portfolio": ui.portfolio_view,
         "performance": ui.performance_view,
         "orders": ui.orders_view,
         "funds": ui.funds_view}[kind](data)
        shown = True

    if shown:
        return

    text = _final_ai_text(messages)
    if text:
        ui.markdown(text)
        return
    ui.warn("The agents finished without an answer. Try rephrasing — or type "
            "'help' to see everything I can do.")


# --------------------------------------------------------------------------- #
# zero-LLM fast path for pure read commands
# --------------------------------------------------------------------------- #
_FILLER = {
    "show", "view", "see", "display", "check", "give", "get", "me", "my", "mine",
    "the", "a", "an", "please", "current", "now", "again", "open", "status",
    "what", "whats", "is", "are", "can", "cant", "t", "you", "i", "want", "to",
    "not", "do", "did", "have", "has", "how", "much", "many", "am", "so", "far",
    "till", "total", "overall", "of",
}
_PORTFOLIO_WORDS = {"portfolio", "holdings", "holding", "pnl", "p&l", "positions"}
_FUNDS_WORDS = {"funds", "fund", "balance", "cash", "margin", "money", "available",
                "buying", "power"}
_ORDERS_WORDS = {"orders", "order", "history", "book", "trades", "recent", "today", "log"}
_PERF_WORDS = {"performance", "stats", "statistics", "booked", "realized", "realised",
               "winrate", "win", "rate", "track", "record", "profit", "loss", "pnl",
               "gains", "net", "positive", "negative"}
_PERF_TRIGGERS = {"performance", "stats", "statistics", "booked", "realized",
                  "realised", "winrate", "win", "gains", "positive", "negative"}

# Help / self-introduction triggers (discoverability — new users type these to
# learn what the assistant can do). Ambiguous words (e.g. "about", which also
# appears in "tell me about Reliance") only match as the WHOLE command; safe
# words match anywhere.
_HELP_WHOLE = {"help", "intro", "about", "menu", "commands", "command", "guide",
               "start", "options", "?"}
_HELP_TOKENS = {"introduce", "yourself", "capabilities", "features", "feature",
                "services"}
_HELP_PHRASES = ("who are you", "what can you do", "what do you do", "what can u do",
                 "tell me about yourself", "about yourself", "who r u",
                 "your features", "your services", "about you and")


def _is_help(command: str) -> bool:
    c = command.strip().lower().rstrip("!.?")
    tokens = set(re.sub(r"[^a-z ]", " ", c).split())
    if c in _HELP_WHOLE:
        return True
    if tokens & _HELP_TOKENS:
        return True
    return any(p in c for p in _HELP_PHRASES)


def _fast_intent(command: str) -> str | None:
    """Map a pure read command to an instant, zero-LLM action; None -> use the
    agents. Deliberately conservative: any unexpected word falls through to the
    LLM so an actual trade instruction is never swallowed by a read-only view."""
    if _is_help(command):
        return "help"
    words = set(re.sub(r"[^a-z& ]", " ", command.lower()).split())
    if not words:
        return None
    extra = words - _FILLER
    if not extra:
        return None
    if extra <= _PORTFOLIO_WORDS:
        return "portfolio"
    if extra <= _FUNDS_WORDS:
        return "funds"
    if extra <= _ORDERS_WORDS and {"orders", "history", "book", "trades"} & extra:
        return "orders"
    if extra <= _PERF_WORDS and (_PERF_TRIGGERS & extra):
        return "performance"
    return None


def _run_fast(intent: str) -> None:
    """Serve read-only intents straight from the broker/UI — no LLM involved."""
    if intent == "help":
        ui.introduce()
        return

    from trinetra import tools  # lazy: broker import stays deferred until needed

    with ui.status("Fetching live data…"):
        if intent == "portfolio":
            data = json.loads(tools.view_portfolio.invoke({}))
        elif intent == "orders":
            data = json.loads(tools.get_order_history.invoke({}))
        elif intent == "performance":
            data = json.loads(tools.get_performance.invoke({}))
        else:
            data = json.loads(tools.get_funds.invoke({}))
    {"portfolio": ui.portfolio_view,
     "orders": ui.orders_view,
     "performance": ui.performance_view,
     "funds": ui.funds_view}[intent](data)


# --------------------------------------------------------------------------- #
# graph invocation + approvals
# --------------------------------------------------------------------------- #
def _invoke(supervisor, payload, config):
    """Invoke the graph, recovering gracefully if the supervisor over-loops.

    The supervisor can occasionally route back to a worker one extra time before
    terminating. Those extra hops generate text only (the order/tool already ran
    exactly once and is HITL-gated), so on a recursion limit we simply read the
    latest state and return it rather than failing the turn."""
    try:
        return supervisor.invoke(payload, config=config)
    except GraphRecursionError:
        log.warning("Supervisor hit the step limit; recovering the latest result.")
        snapshot = supervisor.get_state(config)
        return {"messages": snapshot.values.get("messages", []), "__interrupt__": []}


def _confirm_live() -> bool:
    ui.warn("LIVE TRADING MODE — approved orders will be sent to your real "
            "Groww account with real money.")
    try:
        answer = ui.ask("Type 'I UNDERSTAND' to continue (anything else aborts): ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer == "I UNDERSTAND"


def _order_summary(args: dict) -> str | None:
    """Build a human-readable order line with the resolved symbol, live price and
    estimated total so the user sees exactly what they're approving."""
    from trinetra import instruments, market_data

    raw_symbol = args.get("symbol")
    if not raw_symbol:
        return None
    side = str(args.get("action", "")).upper()
    qty = args.get("quantity")
    otype = str(args.get("order_type", "market")).lower()
    product = (args.get("product") or settings.default_product).upper()

    # Resolve to the real Groww symbol so the price lookup (and the displayed
    # ticker) are correct — this is where "INFOSYS" → "INFY" surfaces.
    rec = instruments.resolve(raw_symbol, args.get("exchange") or None)
    symbol = rec.trading_symbol if rec else raw_symbol
    resolved_note = ""
    if rec and rec.trading_symbol != str(raw_symbol).upper().replace(".NS", "").replace(".BO", ""):
        resolved_note = f"  (resolved '{raw_symbol}' → {rec.trading_symbol}, {rec.name})"

    # Determine the price the order is expected to execute around.
    if otype in ("limit", "sl") and args.get("price"):
        px, px_label = float(args["price"]), "limit"
    elif otype == "sl_m" and args.get("trigger_price"):
        px, px_label = float(args["trigger_price"]), "trigger"
    else:
        px, px_label = (market_data.try_ltp(symbol), "≈ market")

    lines = [f"{side} {qty} × {symbol}  ({otype.upper()}, {product}){resolved_note}"]
    if px:
        total = qty * px if isinstance(qty, (int, float)) else None
        lines.append(f"Price: ₹{px:,.2f} ({px_label})"
                     + (f"   Estimated total: ₹{total:,.2f}" if total else ""))
        if total and total > settings.max_order_value:
            lines.append(f"⚠ Exceeds safety cap ₹{settings.max_order_value:,.0f} — will be blocked.")
    else:
        lines.append("Price: unavailable right now (could not fetch a live quote)")
    return "\n".join(lines)


def _print_approval(interrupts) -> None:
    for intr in interrupts:
        for action in intr.value.get("action_requests", []):
            name = action["name"]
            args = action.get("args", {})
            lines = [f"Tool: {name}"]
            if name == "place_order":
                summary = _order_summary(args)
                if summary:
                    lines.append(summary)
            if args:
                lines.append("Parameters:")
                lines.extend(f"  • {k}: {v}" for k, v in args.items())
            ui.approval_panel(lines, live=settings.is_live)


# --------------------------------------------------------------------------- #
# main loop
# --------------------------------------------------------------------------- #
def run() -> None:
    ui.banner()
    if settings.is_live and not _confirm_live():
        ui.error("Aborted. Set GROWW_TRADING_MODE=paper for simulated trading.")
        return

    # Download/refresh the Groww instrument master in the background while the
    # (slow) LLM clients build — the two together used to cost 20-30s serially.
    from trinetra import instruments

    t0 = time.perf_counter()
    inst_thread = threading.Thread(
        target=instruments.ensure_loaded, name="instruments", daemon=True
    )
    inst_thread.start()

    from trinetra.agents import build_supervisor

    try:
        with ui.status("Starting agents…"):
            supervisor = build_supervisor()
    except Exception as exc:  # noqa: BLE001
        ui.error(f"Failed to start agents: {exc}")
        return

    inst_thread.join(timeout=60)
    if instruments.available():
        ui.success(f"Ready in {time.perf_counter() - t0:.1f}s — agents + "
                   "instrument master loaded.")
    else:
        ui.warn("Instrument master unavailable — using fallback symbol matching.")
    ui.info("New here? Type 'help' for a quick tour. Instant views: 'portfolio', "
            "'performance', 'funds', 'orders'. Or just ask in plain English. "
            "'exit' to quit.")

    while True:
        try:
            command = ui.ask().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            ui.info("Jai Mahakal! 🔱")
            break

        if command.lower() in ("exit", "quit"):
            ui.info("Jai Mahakal! 🔱")
            break
        if not command:
            continue

        turn_start = time.perf_counter()

        # Pure read commands: answer instantly from the broker, no LLM.
        intent = _fast_intent(command)
        if intent:
            try:
                _run_fast(intent)
                ui.timing(time.perf_counter() - turn_start, "instant · no LLM")
            except Exception as exc:  # noqa: BLE001
                log.exception("fast path failed")
                ui.error(str(exc))
            continue

        config = {
            "configurable": {"thread_id": str(uuid.uuid4())},
            "recursion_limit": RECURSION_LIMIT,
        }

        try:
            with ui.status("Thinking…"):
                result = _invoke(
                    supervisor, {"messages": [HumanMessage(content=command)]}, config
                )
        except Exception as exc:  # noqa: BLE001
            ui.error(str(exc))
            continue

        interrupts = result.get("__interrupt__", [])
        if interrupts:
            _print_approval(interrupts)
            try:
                choice = ui.ask("Approve this action? (yes/no): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = "no"
            if choice in ("yes", "y"):
                decision = {"type": "approve"}
                ui.success("Approved — executing…")
            else:
                decision = {"type": "reject"}
                ui.error("Rejected — nothing was executed.")
            try:
                with ui.status("Completing…"):
                    response = _invoke(
                        supervisor, Command(resume={"decisions": [decision]}), config
                    )
                _show_final(response)
            except Exception as exc:  # noqa: BLE001
                ui.error(f"Error completing action: {exc}")
        else:
            _show_final(result)
        ui.timing(time.perf_counter() - turn_start)


if __name__ == "__main__":
    run()
