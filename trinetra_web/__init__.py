"""Web pages served alongside the MCP endpoint.

The landing page explains the product and the guardrails; the linking flow is the
one place credential plaintext is handled, so it never has to travel through an
AI chat.
"""

from trinetra_web.landing import ROUTES as LANDING_ROUTES
from trinetra_web.routes import ROUTES as LINK_ROUTES

ROUTES = [*LANDING_ROUTES, *LINK_ROUTES]

__all__ = ["LANDING_ROUTES", "LINK_ROUTES", "ROUTES"]
