"""Agent + supervisor construction.

Three specialist agents (research, sentiment, trading) coordinated by a
LangGraph supervisor. The trading agent's risky tools are wrapped by the
Human-in-the-Loop middleware so every order/cancel pauses for explicit approval.
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, HumanInTheLoopMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph_supervisor import create_supervisor

from trinetra.config import settings
from trinetra.logging_setup import get_logger
from trinetra.tools import (
    RESEARCH_TOOLS,
    RISKY_TOOLS,
    SENTIMENT_TOOLS,
    TRADING_TOOLS,
    get_live_quote,
)

log = get_logger(__name__)


# === OPENROUTER PATCH (remove this function to revert) ===
def build_openrouter_llm(model: str | None = None) -> BaseChatModel:
    """OpenRouter (OpenAI-compatible) LLM. Used for both supervisor and agents
    when OPENROUTER_API_KEY is set. Model is set via OPENROUTER_MODEL."""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model or settings.openrouter_model,
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        temperature=0,
    )
# === END OPENROUTER PATCH ===


def build_llm() -> BaseChatModel:
    """The agent (worker) LLM. NVIDIA NIM by default (TRINETRA_AGENT_MODEL)."""
    # === OPENROUTER PATCH (remove these 2 lines to revert) ===
    if settings.use_openrouter:
        return build_openrouter_llm()
    # === END OPENROUTER PATCH ===
    from langchain_nvidia_ai_endpoints import ChatNVIDIA

    if not settings.nvidia_api_key:
        raise RuntimeError(
            "NVIDIA_API_KEY is not set. Add it to your .env (the agents need an LLM)."
        )
    return ChatNVIDIA(
        model=settings.agent_model,
        api_key=settings.nvidia_api_key,
        temperature=0,
    )


def build_supervisor_llm(fallback: BaseChatModel) -> BaseChatModel:
    """The supervisor only routes, so prefer a fast tool-calling model (Groq).
    This is the single biggest latency lever — routing happens on every query.
    Falls back to the worker LLM if Groq isn't configured/available."""
    # === OPENROUTER PATCH (remove these 3 lines to revert) ===
    if settings.use_openrouter:
        log.info("Supervisor LLM: OpenRouter %s.", settings.openrouter_model)
        return build_openrouter_llm()
    # === END OPENROUTER PATCH ===
    if settings.use_groq_supervisor and settings.groq_api_key:
        try:
            from langchain_groq import ChatGroq

            log.info("Supervisor LLM: Groq %s (fast routing).", settings.supervisor_model)
            return ChatGroq(
                model=settings.supervisor_model,
                api_key=settings.groq_api_key,
                temperature=0,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Groq supervisor unavailable (%s); using the worker LLM.", exc)
    return fallback


RESEARCH_PROMPT = """
You are a stock research expert for Indian markets (NSE/BSE) via Groww.
STRICT RULE: You MUST call your tools to answer — never invent prices or symbols.
- Use lookup_stocks to convert a company name into a precise trading symbol.
- Use get_live_quote for the current real-time price and day stats.
- Use fetch_stock_data for a fuller snapshot (fundamentals + live price).
Always verify the correct symbol before fetching data. Report the numbers the
tools return; do not estimate.
"""

SENTIMENT_PROMPT = """
You are a market sentiment and technical-analysis expert.
STRICT RULE: ALWAYS call analyze_stock_sentiment for the ticker in question.
When the user asks "should I buy X?", "what's the outlook for X?", etc:
  1. Call analyze_stock_sentiment with the symbol.
  2. Format the result as:
     📊 SYMBOL - SIGNAL (confidence)
     Price: X | RSI: X (signal) | MACD: crossover
     Sentiment: label (score, N headlines)
     Composite Score: X/100
     Stop-loss: X | Target 1: X | Target 2: X
     Summary: 2-sentence synthesis.
"""


def _trading_prompt() -> str:
    mode = settings.trading_mode.value.upper()
    money = (
        "These are REAL orders on the user's live Groww account — real money."
        if settings.is_live
        else "Orders are SIMULATED (paper trading) and logged locally — no real money."
    )
    return f"""
You are the trading-desk agent. Current mode: {mode}. {money}

Your tools:
- place_order: buy/sell equity. order_type = market | limit | sl | sl_m
  (sl/sl_m = stop-loss, live mode only). Requires human approval.
- modify_order / cancel_order: change or cancel a pending live order. Requires approval.
- get_order_status: check whether an order filled.
- get_order_history: show recent orders / order book.
- view_portfolio: show holdings, positions, funds and P&L (ALWAYS call this for any
  portfolio/holdings/P&L question — never answer from memory).
- get_performance: show BOOKED (realized) P&L, win rate, best/worst trades, closed
  positions, today's P&L and sector mix. Call this for "how much profit/loss have I
  booked?", "am I net positive?", "my stats/performance/track record". Paper mode only.
- get_funds: check available buying power before buying.

Rules:
- To BUY/SELL a quantity at market, call place_order directly with symbol, action
  and quantity — it fetches the live price itself. Do NOT ask the user for a price.
- LIMIT: order_type="limit" + price. Stop-loss: order_type="sl" (price+trigger_price)
  or "sl_m" (trigger_price).
- Budget orders ("buy ₹10,000 of X"): call get_live_quote, compute
  floor(budget / price), then place that many shares.
- Default product is CNC (delivery); use MIS only if the user says intraday.

OUTPUT RULES (critical — this app handles real money):
- NEVER invent numbers. Every price, quantity, holding, P&L or funds figure you
  show MUST come from a tool result in this turn. If you don't have it, call the
  tool or say you don't have it — do not guess.
- view_portfolio and get_order_history return a "display" field with a ready-made
  markdown table. Output that "display" value EXACTLY AS-IS. Do NOT rebuild it.
- After place_order / cancel_order / modify_order: reply with ONLY a 1–3 line
  confirmation built from that tool's returned fields (status, symbol, side,
  quantity, price/average_price, estimated_value, order_id). Do NOT also show the
  portfolio, holdings or funds, do NOT print a table, and do NOT add fake UI hints
  like "type 'portfolio'". Stop after the confirmation.
- Never print your planning, reasoning or meta-commentary. Output only the final
  user-facing answer. If a tool returns an error, report it in one line.
"""


class AgentNameMiddleware(AgentMiddleware):
    """Stamp the agent's name onto every AIMessage it produces. The workers'
    models don't set `name` themselves, and the CLI needs it to prefer the
    specialist's own words over the supervisor's (rewrite-prone) relay."""

    def __init__(self, agent_name: str) -> None:
        super().__init__()
        self._agent_name = agent_name

    def after_model(self, state, runtime):  # noqa: ANN001 - framework signature
        msg = state["messages"][-1] if state.get("messages") else None
        if isinstance(msg, AIMessage) and not msg.name:
            # Same id → the messages reducer replaces the original in state.
            return {"messages": [msg.model_copy(update={"name": self._agent_name})]}
        return None


def build_supervisor(checkpointer=None):
    """Build and compile the supervisor graph. Returns the compiled app."""
    llm = build_llm()
    supervisor_llm = build_supervisor_llm(fallback=llm)
    interrupt_on = {t: True for t in RISKY_TOOLS}

    research_agent = create_agent(
        model=llm, tools=RESEARCH_TOOLS, name="research_agent",
        system_prompt=RESEARCH_PROMPT,
        middleware=[AgentNameMiddleware("research_agent")],
    )
    sentiment_agent = create_agent(
        model=llm, tools=SENTIMENT_TOOLS, name="sentiment_agent",
        system_prompt=SENTIMENT_PROMPT,
        middleware=[AgentNameMiddleware("sentiment_agent")],
    )
    # Give the trading agent its own quote tool so it is self-sufficient for
    # market and budget-based orders (no fragile hop through the research agent).
    trading_agent = create_agent(
        model=llm, tools=TRADING_TOOLS + [get_live_quote], name="trading_agent",
        system_prompt=_trading_prompt(),
        middleware=[
            AgentNameMiddleware("trading_agent"),
            HumanInTheLoopMiddleware(interrupt_on=interrupt_on),
        ],
    )

    supervisor = create_supervisor(
        agents=[research_agent, trading_agent, sentiment_agent],
        model=supervisor_llm,
        prompt="""You are a stock trading supervisor. You never call tools yourself —
you route each request to exactly ONE specialist and then relay their answer.

ROUTING RULES (match the user's INTENT, in this priority order):
1. EXECUTION intent — buy, sell, place/modify/cancel an order, see the
   portfolio/holdings/P&L, booked/realized P&L, trade performance/stats/win-rate,
   order history, or funds/buying power
   -> trading_agent. It fetches the live price itself, so do NOT route a
   buy/sell to research first. A buy/sell is complete only once trading_agent has
   placed (or attempted) the order — never stop after merely quoting a price.
2. ADVICE intent — "should I buy X?", "what's your view/outlook on X?",
   sentiment or technical analysis -> sentiment_agent.
3. INFORMATION intent — "what's the price of X?", company info, fundamentals,
   market cap, symbol lookup -> research_agent.

After the specialist responds, relay their answer to the user as-is (especially
any pre-formatted tables) without rewriting it. Never add your own planning,
reasoning, or commentary — output only the final user-facing answer.
NEVER claim that data "has been displayed" or "is shown above" — the user sees
ONLY your message text, so it must contain the specialist's actual answer.
""",
        # full_history: the specialist's ToolMessages (portfolio tables, order
        # results) flow up to the top-level state, so the CLI renders money data
        # deterministically from the tool payloads instead of trusting the
        # supervisor's relay. This is what makes the display hallucination-proof.
        output_mode="full_history",
        add_handoff_messages=False,
        add_handoff_back_messages=False,
    )

    return supervisor.compile(checkpointer=checkpointer or InMemorySaver())
