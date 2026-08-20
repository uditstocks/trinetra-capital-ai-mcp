# Changelog

All notable changes to **Trinetra Capital AI** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> Dates are in `YYYY-MM-DD`. Entries before the formal `1.0.0` line are reconstructed
> from the project's git history and grouped by theme rather than by release tag.

## [Unreleased]

### Added — MCP server (use Trinetra from Claude Desktop / ChatGPT Desktop)
- **`trinetra_mcp` package**: Trinetra is now a Model Context Protocol server, so any
  MCP-capable AI host can drive it in normal conversation. 12 tools, 3 resources
  (`trinetra://portfolio`, `://account`, `://performance`) and 3 prompts
  (`morning_brief`, `portfolio_health`, `should_i_buy`). Runs over stdio, locally —
  nothing to deploy. Needs **no API keys**: the host is the reasoning layer, so no LLM
  library is loaded on this path (see `requirements-mcp.txt`).
- **Two-step orders**: `place_order` only ever returns a priced preview plus a
  single-use `confirmation_token`; `confirm_order` is the only tool that trades. The
  validated instruction is held server-side between the two, so the previewed order is
  exactly the one placed. Tokens expire, are single-use, and are scoped to their user.
- **Reasoning traces**: `analyze_stock` returns an ordered trace of every indicator
  checked, what it read, and how many points it contributed to the composite — so the
  host narrates the real analysis instead of improvising commentary.
- **Per-user sessions** (`trinetra/session.py`): a `SessionContext` plus per-user
  storage and an append-only order audit log replace the process-global settings and
  broker singletons. Trading mode is read from the persisted account record, never from
  a tool argument, so nothing a host reads can flip a session into live trading.
- **Shared service layer** (`trinetra/services/`): the CLI's LangChain tools and the MCP
  tools are both thin adapters over `research` and `trading`, so the two entrypoints
  cannot drift apart.
- **Prompt-injection hygiene**: scraped headlines are stripped of instruction-shaped
  text before appearing in any tool output.
- `pyproject.toml` (installable, with a `trinetra-mcp` entry point) and
  [docs/INSTALL_MCP.md](docs/INSTALL_MCP.md).

### Fixed
- **News sentiment was silently dead**: Yahoo's quote-page scrape returned 404 for every
  ticker, so the sentiment component always contributed 0. Now uses yfinance's news API.

### Added — portfolio analytics & discoverability
- **Realized (booked) P&L** (paper mode): a new `trinetra/analytics.py` replays the
  trade log with the average-cost method to compute booked profit/loss to date —
  total, per-stock, and split into gross profit vs gross loss. Selling shares no
  longer makes the booked gain/loss vanish from view.
- **`performance` view / `get_performance` tool**: booked P&L, an *Overall P&L*
  (realized + unrealized) "am I net positive?" figure, win rate & trade counts,
  best/worst trades, fully-closed positions, today's P&L, and sector allocation.
  Reachable by the `performance` / `stats` / "how much profit have I booked?"
  fast-path, or via the trading agent.
- **Richer portfolio view**: per-holding allocation %, holding period (days), a
  concentration alert when any name exceeds 25%, and a summary line that now shows
  Unrealized, Booked and Overall P&L.
- **`help` / self-introduction command**: typing `help`, `intro`, "introduce
  yourself", "what can you do", etc. prints a professional capabilities tour so new
  users discover every feature and the exact prompts to reach each one.

### Fixed
- **Fail-closed safety cap**: a market order whose notional value cannot be determined
  (no reference price) is now *rejected* instead of bypassing the per-order cap; the
  live broker also self-fetches a reference price for market/SL-M orders.
- **Hallucination-proof CLI output**: portfolio, order-history, funds and order results
  are rendered deterministically from the tool JSON payloads (via
  `output_mode="full_history"`), never from LLM prose. The supervisor can no longer
  claim data "has been displayed" without it appearing, the CLI never echoes the
  user's own message back, and an approved-but-rejected order shows a visible
  rejection panel.
- `GrowwBroker.modify_order` no longer crashes when the broker omits the quantity;
  live funds respect a legitimate ₹0 `clear_cash`; auth-error detection narrowed.
- Technical indicators (RSI/Bollinger/ATR) no longer emit `NaN` into agent-facing JSON.
- Paper broker: correct average-cost basis after sells, oversell and
  insufficient-cash guards, and net worth = cash + market value of holdings.
- Whole-word exchange matching in symbol lookup ("TRANSENSE" ≠ NSE hint).

### Added
- **Rich terminal UI** (`trinetra/ui.py`): coloured portfolio/orders/funds tables,
  order-result and approval panels, spinners, PAPER/LIVE banner; UTF-8-safe on
  Windows; plain-text fallback when `rich` is unavailable.
- **Zero-LLM fast path**: `portfolio`, `orders`, `funds`-style read commands are
  answered instantly straight from the broker (no agent round-trip).
- **Parallelised startup**: the Groww instrument master downloads on a background
  thread while the LLM clients build (`instruments.ensure_loaded` is now thread-safe).
- Offline test suite (`tests/`): safety cap, paper/live broker (mocked Groww SDK),
  agent tool surface, CLI extraction/fast-path, market data, renderers.
- Research-grade documentation suite under [`docs/`](docs/), including a formal
  [research paper](docs/research-paper/RESEARCH_PAPER.md), an architecture reference, a quantitative-analytics
  methodology chapter, a safety/security chapter, and a full API reference.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) with a safety checklist and a documentation-maintenance map.
- This `CHANGELOG.md`.

### Removed
- Dead `GROWW_REQUIRE_CONFIRMATION` / `require_market_confirmation` setting (the HITL
  approval gate is the real control and is always on for risky tools).

---

## [1.0.0] — 2026-06-30

First production-grade release: the system is wired to the **Groww Trading API** for real
order execution, live market data, and portfolio management, with paper trading as the
default and human-in-the-loop safety on every order.

### Added
- **Real Groww broker integration** (`growwapi` + `pyotp`): live equity orders, holdings,
  positions, and funds on NSE/BSE, with daily access-token caching and a transparent
  single re-authentication retry on session expiry.
- **Paper/Live trading modes** selected by `GROWW_TRADING_MODE`, behind a polymorphic
  `Broker` abstraction and a `get_broker()` factory.
- **Layered safety model**: paper-by-default, a hard per-order value cap enforced for both
  modes, human-in-the-loop approval on every `place_order` / `cancel_order` / `modify_order`,
  and an explicit `I UNDERSTAND` live-trading confirmation gate.
- **Authoritative instrument-master resolver** (`trinetra/instruments.py`) that downloads and
  caches Groww's instrument CSV to resolve company names/tickers to exact trading symbols
  (e.g. `INFOSYS → INFY`), replacing unreliable ticker guessing.
- **Groww-first market data** with a yfinance fallback, a short-lived LTP cache, and batched
  quote lookups.
- **Deterministic rendering** of portfolio and order tables in Python (`trinetra/render.py`)
  so figures are never hallucinated by the model.
- **Guided onboarding**: `connect_groww.py` authenticates and runs a read-only profile/funds/
  holdings health check without placing any order.
- **Docker / Docker Compose** support for containerised runs.

### Changed
- Refactored the original single-file prototype into the production `trinetra/` package with a
  clear separation of config, agents, tools, market data, instruments, broker, and rendering.
- Reworked the LLM setup: a fast routing supervisor (Groq) over NVIDIA NIM specialist agents,
  with an optional OpenRouter override for both layers and improved code comments.
- Refined the supervisor routing prompt for cleaner intent-based handoff.

### Added (analytics)
- A dedicated **sentiment & technical-analysis agent** computing RSI, MACD, Bollinger %B and
  ATR with a composite BUY/SELL/HOLD score and ATR-based stop-loss/targets.

---

## Project history (pre-1.0.0)

A condensed timeline reconstructed from git history:

| Date | Milestone |
|------|-----------|
| 2026-06-30 | Groww access, CLI improvements. |
| 2026-06-01 | Stability fix in the run path. |
| 2026-05-08 | LLM setup refactor; README enhancements. |
| 2026-04-30 | Sentiment agent added; supervisor prompt refined; tool corrections. |
| 2026-04-12 | Multi-agent architecture, Docker support, Indian-stock & currency handling; first README. |
| 2026-04-10 → 04-11 | Initial trading agent with human-in-the-loop, portfolio tracking, and project structure. |

[Unreleased]: https://keepachangelog.com/
