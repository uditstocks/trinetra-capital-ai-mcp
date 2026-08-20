"""Minimal, dependency-free structured logging for the whole app.

Call `get_logger(__name__)` anywhere. The first call configures the root
handler using the level from settings. Trade-relevant events should be logged
at INFO so there is always an audit trail of what the agents did.
"""

from __future__ import annotations

import logging
import sys

from trinetra.config import settings

_CONFIGURED = False


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root = logging.getLogger("trinetra")
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure()
    # Namespace everything under "trinetra" so external libs stay quiet.
    short = name.split(".")[-1]
    return logging.getLogger(f"trinetra.{short}")
