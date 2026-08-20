# 🔱 Trinetra Capital AI
### *Multi-Agents. One Market. Zero Missed Moves.*

> An autonomous, multi-agent AI trading system for NSE/BSE — now available as an **MCP server**, so you can run it straight from Claude Desktop or ChatGPT Desktop instead of a terminal.

---

## 🔌 Use it from your AI (recommended)

Talk to Trinetra inside the AI you already use. No API keys, no accounts, nothing to deploy.

```bash
pip install -r requirements-mcp.txt
```

Then add this to `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) and restart the app:

```json
{
  "mcpServers": {
    "trinetra": {
      "command": "python",
      "args": ["-m", "trinetra_mcp"],
      "env": { "PYTHONPATH": "/path/to/Trinetra-Capital-AI" }
    }
  }
}
```

Now just talk:

```
set me up for paper trading
how does Infosys look right now?      → full reasoning trace, not a vibe
buy 10 shares of HCL                  → preview first, places nothing until you say yes
show my portfolio
how much profit have I booked?
```

**Prices and analysis are real and live — only the money is simulated.** Every order takes two
steps: your AI shows a preview, and nothing is placed until you explicitly approve it.

📖 Full setup, ChatGPT Desktop, Claude Code and troubleshooting: **[docs/INSTALL_MCP.md](docs/INSTALL_MCP.md)**

---

## ⚡ What is Trinetra Capital AI?

Trinetra Capital AI orchestrates specialised AI agents under a central supervisor to research stocks, analyse sentiment, and **place real buy/sell orders through your Groww account**. It ships in **paper mode by default** so you can exercise the entire system safely, and flips to **live trading** with a single environment variable.

- 📡 **Live market data** — real-time quotes, LTP and OHLC from Groww (yfinance fallback).
- 💼 **Real portfolio** — holdings, positions, funds and live P&L from your Groww account.
- 🛒 **Real order execution** — market & limit orders (CNC delivery / MIS intraday) on NSE/BSE.
- 🛡️ **Human-in-the-loop** — every order/cancel pauses for explicit approval.
- 🧯 **Safety rails** — paper-by-default, a hard per-order value cap, and a "LIVE" confirmation gate.

---

## 🏗️ System Architecture

```
                         USER (CLI)  ──>  main.py
                                │
                                ▼
              ┌─────────────────────────────────────────┐
              │            SUPERVISOR AGENT              │
              │   Routes by intent: execute / advise /   │
              │   inform. Never calls tools itself.      │
              └───────┬───────────────┬─────────────┬────┘
                      │               │             │
              research_agent   sentiment_agent   trading_agent
                      │               │             │  (HITL gate)
                      ▼               ▼             ▼
   ┌──────────────────────────────────────────────────────────────┐
   │                      TOOLS (trinetra/tools.py)                │
   │  lookup_stocks · get_live_quote · fetch_stock_data            │
   │  analyze_stock_sentiment                                       │
   │  place_order* · cancel_order* · get_order_status              │
   │  view_portfolio · get_funds          (* = human approval)     │
   └───────────────┬───────────────────────────────┬──────────────┘
                   ▼                                ▼
         ┌───────────────────┐            ┌────────────────────────┐
         │   MARKET DATA      │            │     BROKER LAYER        │
         │ Groww live quote   │            │  get_broker() factory   │
         │   ⟶ yfinance       │            │ ┌────────┐  ┌─────────┐ │
         │ fundamentals (yf)  │            │ │ Paper  │  │  Groww  │ │
         │ technical+sentiment│            │ │ broker │  │ broker  │ │
         └─────────┬──────────┘            │ └────────┘  └────┬────┘ │
                   │                       └──────────────────┼──────┘
                   ▼                                          ▼
            yfinance / Groww                    Groww Trading API (growwapi)
                                                 auth + daily token cache
                                                 (trinetra/broker/groww_client.py)
```

**Mode switch:** `GROWW_TRADING_MODE=paper` → `PaperBroker` (simulated fills + portfolio in `portfolio.json`). `=live` → `GrowwBroker` (real orders + your real Groww holdings). Orders **and** the portfolio you see follow the mode: demo mode shows the demo portfolio, live mode shows your real Groww account. **Market data (quotes/prices) is always real and live** from Groww (or yfinance fallback) in *both* modes — only orders and portfolio are simulated in demo. Switching is one line in `.env` + a restart.

---

## 🔌 Connect your Groww account (2 minutes)

```bash
pip install -r requirements.txt        # installs growwapi + pyotp + everything else
cp .env.example .env                    # then add your keys (see below)
python connect_groww.py                 # guided setup + read-only health check
```

`connect_groww.py` walks you through getting credentials, authenticates, caches a daily token, and prints your profile, funds and holdings to confirm the connection — **without placing any order**.

### Getting Groww API credentials
1. Open <https://groww.in/trade-api/docs> and log in.
2. Go to **Groww Cloud / API Keys → Generate API Key**.
3. Pick one flow and put the values in `.env`:

   **A) TOTP flow (recommended — token auto-rotates daily):**
   ```env
   GROWW_API_KEY=your_totp_token
   GROWW_TOTP_SECRET=your_totp_secret
   ```
   **B) Approval flow (API key + secret):**
   ```env
   GROWW_API_KEY=your_api_key
   GROWW_API_SECRET=your_api_secret
   ```

> No Groww keys yet? The system still runs — research/sentiment use yfinance and orders are paper-traded. Connect Groww whenever you're ready.

---

## 🚀 Run it

```bash
python main.py            # interactive multi-agent CLI
```

You'll see a banner showing the mode (PAPER/LIVE), Groww connection status and the per-order safety cap. In **LIVE** mode you must type `I UNDERSTAND` before the session starts.

### Going live
```env
GROWW_TRADING_MODE=live          # default is 'paper'
GROWW_MAX_ORDER_VALUE=100000     # hard rupee ceiling per order
GROWW_DEFAULT_PRODUCT=CNC        # CNC = delivery, MIS = intraday
```

---

## 🔑 Environment Variables

| Variable | Required | Description |
|---|---|---|
| `NVIDIA_API_KEY` | ✅ | LLM that powers the agents |
| `GROWW_API_KEY` | for Groww | TOTP token or API key |
| `GROWW_TOTP_SECRET` | flow A | TOTP secret (auto-generates daily token) |
| `GROWW_API_SECRET` | flow B | API secret (approval flow) |
| `GROWW_TRADING_MODE` | – | `paper` (default) or `live` |
| `GROWW_MAX_ORDER_VALUE` | – | Per-order rupee cap (default 100000) |
| `GROWW_DEFAULT_PRODUCT` | – | `CNC` (delivery) or `MIS` (intraday) |
| `GROWW_DEFAULT_EXCHANGE` | – | `NSE` (default) or `BSE` |
| `TRINETRA_PAPER_CASH` | – | Virtual cash for paper mode |
| `GROQ_API_KEY` | – | Optional alternate LLM |

See [.env.example](.env.example) for the full annotated list.

---

## 💬 Example Interactions

```
> what's the price of Reliance?
  → research_agent → get_live_quote → ₹1,328.80 (+1.67%), prev close ₹1,307.00

> should I buy Infosys?
  → sentiment_agent → analyze_stock_sentiment → BUY (composite 72/100), SL/targets

> buy 2 shares of Reliance at market
  → trading_agent → place_order(RELIANCE, buy, 2, market)
  → ⚠️ HITL gate: "Approve buy 2 × RELIANCE @ market? (yes/no)"
  → Fills at ₹1,328.80 — logged (paper) or sent to Groww (live)

> buy ₹10,000 worth of TCS
  → trading_agent → get_live_quote → floor(10000/price) → place_order → HITL

> show my portfolio
  → trading_agent → view_portfolio → clean holdings table, live P&L, summary

> show my orders today
  → trading_agent → get_order_history → order book table

> set a stop-loss sell on 5 TCS at trigger 3800   (LIVE only)
  → trading_agent → place_order(order_type="sl_m", trigger_price=3800) → HITL

> how much buying power do I have?
  → trading_agent → get_funds → available cash / margin
```

> Routing runs on a fast Groq model (`meta/llama-3.3-70b-instruct`) while the
> specialist agents use NVIDIA NIM — cutting a typical query from ~30s to well
> under 10s. Portfolio/order tables are rendered deterministically in Python so
> they're always clean and never hallucinated.

---

## 📁 Project Structure

```
Trinetra-Capital-AI/
├── main.py                      # entrypoint  →  python main.py
├── connect_groww.py             # guided Groww setup + health check
├── trinetra/
│   ├── config.py                # settings, paper/live mode, safety caps
│   ├── logging_setup.py         # structured logging
│   ├── symbols.py               # yfinance ⟷ Groww symbol normalisation
│   ├── market_data.py           # Groww live data + yfinance fallback + TA/sentiment
│   ├── tools.py                 # LangChain tools (the agents' only surface)
│   ├── agents.py                # agents + supervisor (HITL on risky tools)
│   ├── cli.py                   # interactive loop + approval gate
│   └── broker/
│       ├── base.py              # Broker interface + normalised data types
│       ├── groww_client.py      # Groww auth + daily access-token cache
│       ├── groww_broker.py      # live broker (real orders/portfolio/funds)
│       └── paper_broker.py      # simulated broker (portfolio.json)
├── requirements.txt
├── .env.example
└── Dockerfile / docker-compose.yml
```

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Agent framework | LangGraph, LangChain |
| Multi-agent | `langgraph-supervisor` (hierarchical supervisor) |
| LLM (agents) | NVIDIA NIM (`nemotron-3-super-120b`) |
| LLM (supervisor) | Groq (`meta/llama-3.3-70b-instruct`) — fast routing |
| **Broker** | **Groww Trading API (`growwapi`) + `pyotp`** |
| Market data | Groww live (quote/LTP/OHLC) + `yfinance` fallback |
| HITL | `HumanInTheLoopMiddleware` (interrupt-based approval) |
| Memory | LangGraph checkpointing (`InMemorySaver`) |
| Containerisation | Docker + Docker Compose |

---

## 🐳 Docker

```bash
docker-compose build
docker-compose run trading-agent      # runs python main.py
```
`.env` is injected and `portfolio.json` is volume-mounted.

---

## 🗺️ Roadmap

| Feature | Status |
|---|---|
| **Real Broker API (Groww)** — live orders, portfolio, funds, market data | ✅ **Done** |
| News Sentiment Analysis | ✅ Done |
| Paper / Live safety modes + per-order cap | ✅ Done |
| PostgreSQL persistent memory (`PostgresSaver`) | 🔄 Planned |
| Groww live websocket feed (streaming ticks) | 🔄 Planned |
| Portfolio P&L engine + alerts | 🔄 Planned |
| F&O / commodity segments | 🔄 Planned |
| Cloud deployment + scheduled scans | 🔄 Planned |

---

## ⚠️ Disclaimer

This software can place **real orders with real money** when `GROWW_TRADING_MODE=live`. It is provided for educational and personal-automation use, **without warranty**. Algorithmic/automated trading carries significant financial risk — you are solely responsible for every order it places. Start in **paper mode**, keep `GROWW_MAX_ORDER_VALUE` conservative, review every human-in-the-loop prompt, and consult a SEBI-registered advisor before trading. Not investment advice.

---

Built with 🔱 by **Udit** — *"Har decision mein teen nazar — research ki, trading ki, aur insaan ki."*
