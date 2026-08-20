# 🤝 Contributing to Trinetra Capital AI

Thank you for your interest in Trinetra Capital AI. This guide explains how to contribute code **and** — just as importantly — how to keep the [documentation](docs/) accurate as the project evolves. Because Trinetra can place **real-money orders**, contributions that touch the broker, order, or safety paths are held to a higher bar.

> **Golden rule:** the safe default must stay safe. Never weaken paper-by-default, the per-order cap, or the human-in-the-loop approval gate without an explicit, reviewed decision.

---

## 1. Project layout

```
Trinetra-Capital-AI/
├── main.py                  # entrypoint → python main.py
├── connect_groww.py         # guided Groww onboarding + read-only health check
├── trinetra/                # the application package (see docs/10-api-reference.md)
│   ├── config.py            # single source of truth for settings & safety
│   ├── agents.py            # supervisor + specialist agents
│   ├── cli.py               # interactive loop + HITL approval gate
│   ├── tools.py             # the LLM-facing tool surface
│   ├── market_data.py       # quotes, fundamentals, technicals, sentiment
│   ├── instruments.py       # authoritative Groww instrument-master resolver
│   ├── symbols.py           # pure symbol normalisation
│   ├── render.py            # deterministic table rendering
│   ├── logging_setup.py     # structured logging
│   └── broker/              # broker abstraction (paper + Groww live)
├── docs/                    # the documentation set (this is what you maintain)
├── Dockerfile / docker-compose.yml
└── requirements.txt
```

A deeper tour lives in [docs/02-system-architecture.md](docs/02-system-architecture.md).

---

## 2. Development setup

```bash
git clone <your-fork-url>
cd Trinetra-Capital-AI
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # fill in your keys
```

**Always develop and test in paper mode** (`GROWW_TRADING_MODE=paper`, the default). Use `python connect_groww.py` to confirm a read-only Groww connection without placing any order. See [docs/08-installation-and-deployment.md](docs/08-installation-and-deployment.md).

---

## 3. Coding conventions

- **Single source of truth.** Read configuration through `trinetra.config.settings`; do not read `os.environ` for trading behaviour anywhere else.
- **Tools are the only LLM-facing surface.** Agents must never touch brokers or data sources directly — add capability as a `@tool` in `trinetra/tools.py`.
- **Respect the broker abstraction.** New broker behaviour goes behind the `Broker` interface in `trinetra/broker/base.py`; the agents and tools must not know which broker is active.
- **Never let the model invent numbers.** Money, prices, quantities and P&L must come from a tool result and, where shown to the user, be rendered deterministically in `trinetra/render.py`.
- **Fail safe.** Validate before acting; degrade gracefully (yfinance fallback, stale cache, single re-auth) rather than crashing a turn.
- **Type hints + docstrings.** Match the existing style. Tool docstrings are prompts the LLM reads — keep them precise.
- **Logging, not prints, inside the package.** Use `get_logger(__name__)`; reserve `print` for the CLI.

---

## 4. Safety checklist for risky changes

If your change touches `trinetra/broker/**`, `trinetra/tools.py` (order tools), `trinetra/cli.py` (approval gate), or `trinetra/config.py` (safety settings), confirm **all** of the following before opening a PR:

- [ ] Paper mode remains the default; live mode still requires explicit opt-in.
- [ ] `Broker.guard_order` (the per-order value cap) still runs for **both** paper and live orders.
- [ ] `place_order`, `cancel_order`, and `modify_order` remain in `RISKY_TOOLS` and stay HITL-gated.
- [ ] The live `I UNDERSTAND` confirmation gate is intact.
- [ ] Symbols are still resolved against the instrument master before any order reaches a broker.
- [ ] You verified the behaviour in **paper mode** end-to-end.

---

## 5. Commit & pull-request workflow

- Branch from `main`; keep commits focused and descriptive.
- Reference the affected area in the subject line (e.g. `broker: ...`, `agents: ...`, `docs: ...`).
- In the PR description, state **what changed, why, and how you verified it** (paper-mode steps).
- Flag any change to the safety model explicitly so reviewers can scrutinise it.

---

## 6. Keeping the documentation current (please read)

Documentation is a first-class deliverable here, not an afterthought. **Code changes that alter observable behaviour must update the docs in the same PR.** Use this map to find the page(s) to touch:

| If you change… | Update… |
|---|---|
| Agents, prompts, routing, or HITL | [docs/03-multi-agent-system.md](docs/03-multi-agent-system.md) |
| Brokers, order types, order lifecycle | [docs/04-execution-and-broker-layer.md](docs/04-execution-and-broker-layer.md) |
| Quotes, indicators, scoring, instruments | [docs/05-market-data-and-quant-analytics.md](docs/05-market-data-and-quant-analytics.md) |
| Safety controls or security handling | [docs/06-safety-risk-and-security.md](docs/06-safety-risk-and-security.md) |
| Any environment variable or default | [docs/07-configuration-reference.md](docs/07-configuration-reference.md) |
| Setup, dependencies, Docker | [docs/08-installation-and-deployment.md](docs/08-installation-and-deployment.md) |
| User-facing commands or interactions | [docs/09-usage-guide.md](docs/09-usage-guide.md) |
| Any public function/class signature | [docs/10-api-reference.md](docs/10-api-reference.md) |
| New terms or dependencies | [docs/13-glossary-and-references.md](docs/13-glossary-and-references.md) |
| Released changes | [CHANGELOG.md](CHANGELOG.md) |

**Documentation style** (so the set stays consistent):

- Markdown, one `#` H1 per file, a short abstract, then a per-document table of contents.
- Use Mermaid diagrams for architecture/flows and Markdown tables for references.
- Cite code by relative path (e.g. `trinetra/agents.py`) and use inline code for symbols, env vars, and values.
- Keep the previous/next navigation footer and the cross-links accurate.
- When in doubt, **prefer the code over prose** — the source is the ground truth.

Add a one-line entry to [CHANGELOG.md](CHANGELOG.md) under *Unreleased* for any user-visible change.

---

## 7. Reporting issues

Open an issue describing the behaviour, the mode (paper/live), the exact command, and the relevant log lines (logs are namespaced under `trinetra`). For anything that could affect real money, label it clearly and **do not** include real API keys or tokens.

---

Built with 🔱 — *"Har decision mein teen nazar — research ki, trading ki, aur insaan ki."*
