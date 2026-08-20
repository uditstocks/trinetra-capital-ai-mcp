"""
Central configuration for Trinetra Capital AI.

Everything tunable lives here and is sourced from environment variables (loaded
from `.env`). Nothing else in the codebase should read `os.environ` for trading
behaviour — import `settings` instead so there is a single source of truth.

Safety model
------------
- TRADING_MODE defaults to "paper". Real orders only ever reach Groww when the
  user explicitly sets GROWW_TRADING_MODE=live. Market data and portfolio reads
  are always served from the real Groww account when credentials are present.
- MAX_ORDER_VALUE is a hard rupee ceiling enforced before any order (paper or
  live) is placed, so a hallucinated quantity can never blow up the account.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root regardless of where the process is launched.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


class TradingMode(str, Enum):
    PAPER = "paper"  # simulated fills logged to portfolio.json, no real money
    LIVE = "live"    # orders are sent to the real Groww account


class AuthMethod(str, Enum):
    TOTP = "totp"        # GROWW_API_KEY (TOTP token) + GROWW_TOTP_SECRET
    APPROVAL = "approval"  # GROWW_API_KEY + GROWW_API_SECRET
    NONE = "none"        # no Groww credentials configured


def _get(name: str, default: str | None = None) -> str | None:
    """Read an env var, trimming the surrounding whitespace/quotes that creep
    into hand-edited .env files (e.g. `KEY = "value"`)."""
    raw = os.getenv(name, default)
    if raw is None:
        return None
    return raw.strip().strip('"').strip("'").strip()


def _get_bool(name: str, default: bool = False) -> bool:
    val = _get(name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    val = _get(name)
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # --- Groww credentials ---
    groww_api_key: str | None = field(default_factory=lambda: _get("GROWW_API_KEY"))
    groww_api_secret: str | None = field(default_factory=lambda: _get("GROWW_API_SECRET"))
    groww_totp_secret: str | None = field(default_factory=lambda: _get("GROWW_TOTP_SECRET"))

    # --- Trading behaviour / safety ---
    trading_mode: TradingMode = field(
        default_factory=lambda: TradingMode(
            (_get("GROWW_TRADING_MODE", "paper") or "paper").lower()
        )
    )
    default_product: str = field(
        default_factory=lambda: (_get("GROWW_DEFAULT_PRODUCT", "CNC") or "CNC").upper()
    )
    default_exchange: str = field(
        default_factory=lambda: (_get("GROWW_DEFAULT_EXCHANGE", "NSE") or "NSE").upper()
    )
    max_order_value: float = field(
        default_factory=lambda: _get_float("GROWW_MAX_ORDER_VALUE", 100_000.0)
    )

    # --- LLM credentials ---
    nvidia_api_key: str | None = field(default_factory=lambda: _get("NVIDIA_API_KEY"))
    groq_api_key: str | None = field(default_factory=lambda: _get("GROQ_API_KEY"))
    agent_model: str = field(
        default_factory=lambda: _get(
            "TRINETRA_AGENT_MODEL", "meta/llama-3.3-70b-instruct"
        )
    )
    # The supervisor only routes, so a fast tool-calling model (Groq) keeps
    # latency low. Falls back to the agent LLM if Groq is unavailable.
    supervisor_model: str = field(
        default_factory=lambda: _get("TRINETRA_SUPERVISOR_MODEL", "meta/llama-3.3-70b-instruct")
    )
    use_groq_supervisor: bool = field(
        default_factory=lambda: _get_bool("TRINETRA_GROQ_SUPERVISOR", True)
    )

    # === OPENROUTER PATCH (remove this block to revert) ===
    # When OPENROUTER_API_KEY is set, OpenRouter powers BOTH the supervisor and the
    # agents (overriding NVIDIA/Groq). Toggle off with TRINETRA_USE_OPENROUTER=false.
    openrouter_api_key: str | None = field(default_factory=lambda: _get("OPENROUTER_API_KEY"))
    openrouter_model: str = field(
        default_factory=lambda: _get("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    )
    openrouter_base_url: str = field(
        default_factory=lambda: _get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    )
    use_openrouter_flag: bool = field(
        default_factory=lambda: _get_bool("TRINETRA_USE_OPENROUTER", True)
    )
    # === END OPENROUTER PATCH ===

    # --- Files ---
    portfolio_file: Path = field(
        default_factory=lambda: PROJECT_ROOT
        / (_get("TRINETRA_PORTFOLIO_FILE", "portfolio.json") or "portfolio.json")
    )
    token_cache_file: Path = field(
        default_factory=lambda: PROJECT_ROOT / ".groww_token_cache.json"
    )
    paper_starting_cash: float = field(
        default_factory=lambda: _get_float("TRINETRA_PAPER_CASH", 1_000_000.0)
    )

    log_level: str = field(
        default_factory=lambda: (_get("TRINETRA_LOG_LEVEL", "INFO") or "INFO").upper()
    )

    # ------------------------------------------------------------------ #
    @property
    def is_live(self) -> bool:
        return self.trading_mode is TradingMode.LIVE

    # === OPENROUTER PATCH (remove this property to revert) ===
    @property
    def use_openrouter(self) -> bool:
        return bool(self.openrouter_api_key) and self.use_openrouter_flag
    # === END OPENROUTER PATCH ===

    @property
    def auth_method(self) -> AuthMethod:
        if self.groww_api_key and self.groww_totp_secret:
            return AuthMethod.TOTP
        if self.groww_api_key and self.groww_api_secret:
            return AuthMethod.APPROVAL
        return AuthMethod.NONE

    @property
    def groww_configured(self) -> bool:
        """True when we have enough credentials to authenticate with Groww."""
        return self.auth_method is not AuthMethod.NONE

    def validate_for_live(self) -> list[str]:
        """Return a list of human-readable problems that would block live trading.
        Empty list means good to go."""
        problems: list[str] = []
        if not self.groww_configured:
            problems.append(
                "No Groww credentials found. Set GROWW_API_KEY plus either "
                "GROWW_TOTP_SECRET (TOTP flow) or GROWW_API_SECRET (approval flow)."
            )
        if self.default_product not in ("CNC", "MIS"):
            problems.append(
                f"GROWW_DEFAULT_PRODUCT={self.default_product!r} is not supported in v1 "
                "(use CNC for delivery or MIS for intraday)."
            )
        if self.max_order_value <= 0:
            problems.append("GROWW_MAX_ORDER_VALUE must be a positive number.")
        return problems


# Singleton imported across the codebase.
settings = Settings()
