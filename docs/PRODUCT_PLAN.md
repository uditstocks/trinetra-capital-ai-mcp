# Trinetra Capital AI — MCP Product Plan

**What we are building:** Trinetra stops being a local CLI and becomes an **MCP server** that any
MCP-capable AI host (Claude Desktop, ChatGPT Desktop, Claude Code, others) connects to. The user
chats normally — *"find me a well-performing stock"*, *"buy 10 shares of HCL"* — and Trinetra's
agent tooling does the research and places the order.

**Product thesis:** users should feel **agentic financial intelligence**, not a thin API wrapper.
Every analysis tool returns its *reasoning trace* (indicators checked, what they said, how the
verdict was formed), so the host AI narrates Trinetra's thinking instead of inventing its own.

---

## Architecture shift (why this is not just "wrap the tools")

When the host AI is the orchestrator, **our LangGraph supervisor becomes redundant** — Claude/GPT
already decides which tool to call. What we keep and deepen is the *deterministic* intelligence:
indicator math, composite scoring, symbol resolution, order validation, safety caps.

| Today (CLI) | Target (MCP product) |
|---|---|
| LangGraph supervisor routes intent | Host AI routes; supervisor dropped from MCP path |
| `settings` frozen singleton from `.env` | Per-user `SessionContext` resolved per call |
| `get_broker()` module-level global | `get_broker(ctx)` per user + mode |
| One `portfolio.json` for everyone | Per-user isolated store |
| HITL = blocking CLI prompt | Two-step confirm token (`place_order` → `confirm_order`) |
| One user, one `.env` | Multi-tenant with encrypted credential vault (Phase 2) |

### The 5 concrete blockers to fix first
1. `trinetra/config.py` — `settings = Settings()` global, loaded from `.env` at import.
2. `trinetra/broker/__init__.py` — `_broker` module-level singleton.
3. `trinetra/config.py` — single `portfolio_file` path.
4. `trinetra/tools.py` — every tool calls `get_broker()` with no user context.
5. HITL lives in the agent/CLI layer — has no equivalent in MCP.

---

## Safety invariants (never violated, any phase)

- Paper is the default. Live requires an explicit, separate, per-user activation.
- Per-order value cap enforced **inside the broker layer**, fails *closed* when value is unknowable.
- No order executes on a single tool call. Preview → explicit user confirmation → execute.
- Broker credentials are **never** typed into the chat box, never logged, never in tool output.
- Every order attempt is written to an immutable audit log before it reaches the broker.
- Tool output is data. Text inside market data / news / filings is **never** treated as instruction.

---

# Phase 1 — INITIAL
### Goal: working MCP, paper trading only, zero broker credentials

**Ship criteria** — a stranger can:
1. Add Trinetra to Claude Desktop or ChatGPT Desktop via config, restart, and see the tools.
2. Say *"set me up"* → gets a paper account with virtual cash.
3. Ask *"how is Reliance looking?"* → gets real live data + Trinetra's reasoning trace.
4. Say *"buy 10 HCL"* → sees a preview, confirms, gets a simulated fill.
5. Ask *"show my portfolio / performance"* → real numbers, deterministically rendered.
6. Run the same flows in the CLI, unchanged.

**In scope**
- MCP server (Python SDK / FastMCP), **stdio** transport, local install.
- Session-context refactor + per-user paper storage (local, file or SQLite).
- Service layer extracted from LangChain `@tool` wrappers → shared by CLI and MCP.
- Full read-only tool surface + reasoning-trace research tools.
- Two-step order confirmation with a single-use, TTL-bound, parameter-bound token.
- MCP **resources** (portfolio as readable context) and **prompts** (e.g. "morning brief").
- Prompt-injection guardrails at the tool boundary.
- Tests + install docs for both hosts.

**Out of scope** — real brokers, OAuth, hosting, billing, Zerodha, websockets.

**Work breakdown** → see [phase-1-initial.tasks.json](phase-1-initial.tasks.json) (14 sequenced tasks with acceptance criteria).

**Phase 1 risks**
| Risk | Mitigation |
|---|---|
| Host AI calls `confirm_order` on its own without asking the user | Token must be echoed back by the user; tool description states it explicitly; log every auto-confirm attempt |
| ChatGPT Desktop MCP support differs from Claude's | Build to spec, test both, document per-host quirks; Claude is the primary target |
| Refactor breaks the working CLI | Task order puts CLI regression tests before MCP work; CLI stays on the same service layer |

---

# Phase 2 — PRO
### Goal: real money, real brokers, multiple users, hosted

**Ship criteria** — a user connects the MCP by **URL** (no local install), logs in, links a Groww
**or** Zerodha account through a secure hosted page, flips to live, and places a real order.

**Scope**
1. **Remote MCP** — Streamable HTTP transport + OAuth 2.1 authorization. This is what makes
   "paste a URL and connect" possible for ChatGPT and Claude.
2. **Multi-tenancy** — Postgres: users, accounts, broker links, sessions, orders, audit log.
   Every tool call resolves `user_id → account → broker` before touching anything.
3. **Credential vault** — envelope encryption (KMS/managed secret store), per-user keys, at-rest
   encryption, rotation, zero credential material in logs or tool responses.
4. **Secure broker linking** — tool returns a one-time signed URL to a page we host. Credentials
   are entered *there*, never in chat. Same mechanism for both brokers.
5. **Zerodha adapter** — implement the existing `Broker` ABC (`trinetra/broker/base.py`).
   Note: Kite Connect uses a browser redirect login, unlike Groww's TOTP — the secure-link flow
   must handle both. Add a broker registry so a third broker is a plug-in, not a refactor.
6. **Live-mode gates** — separate live activation step, per-user order cap, daily notional limit,
   daily order-count limit, and a user-accessible kill switch that hard-disables live instantly.
7. **Immutable audit log** — who, when, which account, what params, broker response, for every order.
8. **Reconciliation** — periodic broker-state sync so Trinetra's view never silently drifts.

**Blocking prerequisites (compliance track — start now, runs in parallel)**
- [ ] Groww API terms: is multi-user / redistribution permitted, or partner-only?
- [ ] Zerodha (Kite Connect) terms: same question.
- [ ] SEBI retail algo-trading framework: does routing orders for other users require exchange
      registration of the strategy + an algo ID via the broker?
- [ ] Positioning decision: **"execute what the user explicitly instructed"** (safer) vs.
      **"we give buy/sell recommendations"** (may implicate SEBI RA/IA registration).
- [ ] Legal entity + ToS + limitation of liability. A README disclaimer is not sufficient here.

> ⚠️ Any of the first three can reshape the architecture. Resolve them **before** building the
> credential vault or the Zerodha adapter.

---

# Phase 3 — BEYOND
### Goal: hardened, optimized, launch-ready

**Ship criteria:** load-tested, security-reviewed, observable, and a signed-off go/no-go.

**Scope**
1. **Performance** — async I/O, instrument master in DB not CSV, quote caching, connection pooling.
2. **Reliability** — order idempotency keys, backoff retries, per-broker circuit breakers,
   graceful degradation, per-user rate limits.
3. **Observability** — metrics, tracing, alerting on failed live orders. You must be able to answer
   *"what happened to user X's order"* in under a minute.
4. **Security hardening** — external review/pen test, secret rotation, dependency scanning, and
   **prompt-injection defense in depth** (the host LLM reads attacker-influenceable news/web text;
   a poisoned headline must never be able to trigger a trade).
5. **Advanced product** — websocket streaming, watchlists + alerts, backtesting, portfolio optimization.
6. **Commercial** — pricing tiers, usage metering, billing, support.
7. **Launch** — staged rollout (private beta → waitlist → open), incident runbook, rollback plan.

**Go/no-go checklist**
- [ ] Compliance items from Phase 2 all resolved in writing
- [ ] Security review passed, no open high/critical findings
- [ ] Kill switch tested under load
- [ ] Audit log verified complete and tamper-evident
- [ ] Live order path tested with real (small) money by the founder
- [ ] Incident runbook + rollback rehearsed
- [ ] ToS, privacy policy, risk disclosure published

---

## Sequencing rule

Each phase ships **working software**. Phase 1 is usable and demoable on its own (paper trading is
a legitimate product). Do not start Phase 2 engineering until the Phase 1 ship criteria pass and the
compliance track has answers. Do not launch until the Phase 3 go/no-go is fully checked.
