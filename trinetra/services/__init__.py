"""Service layer shared by the CLI and the MCP server.

Plain functions in, plain dicts out — no LangChain, no MCP, no LLM calls. Both
entrypoints call these, so product behaviour lives in one place.

    research  lookups, quotes and analysis      (no account needed)
    trading   portfolio, funds and orders       (needs a SessionContext)
"""

from trinetra.services import research, trading

__all__ = ["research", "trading"]
