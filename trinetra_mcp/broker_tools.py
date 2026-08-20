"""Tools for connecting a real broker and controlling live trading.

Two principles run through every tool here:

- **No credential ever crosses this surface.** `link_broker` hands back a URL and
  nothing else; there is no parameter anywhere in this module that accepts a key,
  a secret, or a token belonging to a broker.
- **Linking and going live are separate decisions.** Connecting an account only
  grants the ability to read it. Real orders need `switch_trading_mode('live')`
  as a deliberate, separate step, and can be stopped instantly with the kill
  switch.
"""

from __future__ import annotations

import os
from typing import Any

from trinetra import brokerlink, limits, store, vault
from trinetra.broker import registry
from trinetra.logging_setup import get_logger
from trinetra_mcp.runtime import (
    ToolInputError,
    choice,
    current_context,
    non_negative_float,
    quiet_stdout,
    require_account,
)

log = get_logger(__name__)


def public_url() -> str:
    return os.getenv("PUBLIC_URL", "").rstrip("/")


def hosted() -> bool:
    """Broker linking exists only on the hosted server, which has the database
    and the vault. The local server trades paper and says so."""
    return bool(store.database_url() and public_url())


LOCAL_ONLY = {
    "status": "unavailable",
    "message": "This Trinetra server runs locally in paper mode and has no broker "
               "linking. Connect to the hosted Trinetra to trade a real account.",
}


def register(mcp) -> None:
    @mcp.tool()
    def list_brokers() -> dict[str, Any]:
        """List the real brokers Trinetra can connect to, and how each one links.

        Use when the user asks which brokers are supported, or before link_broker
        so they can choose.
        """
        return {
            "brokers": brokerlink.supported_brokers(),
            "note": "Credentials are entered on a secure page, never in this chat.",
        }

    @mcp.tool()
    def link_broker(broker: str) -> dict[str, Any]:
        """Start connecting the user's real Groww or Zerodha account.

        Returns a one-time secure URL. Give the user that URL and ask them to open
        it — they enter their broker API keys there, on a page served over HTTPS.

        NEVER ask the user for API keys, secrets, TOTP codes or tokens in this
        conversation, and never accept them if offered: anything typed in chat is
        stored in conversation history. If the user pastes a credential, tell them
        to rotate it at their broker and use this link instead.

        Linking only lets Trinetra read the account and prepare orders. Real
        trading still requires switch_trading_mode('live') afterwards.

        `broker` is "groww" or "zerodha".
        """
        if not hosted():
            return LOCAL_ONLY
        ctx = current_context()
        if (missing := require_account(ctx)) is not None:
            return missing
        try:
            name = choice(broker, "broker", tuple(brokerlink.BROKERS))
        except ToolInputError as exc:
            return {"status": "error", "error": str(exc)}

        try:
            with quiet_stdout():
                request = brokerlink.create_link(ctx, name)
        except (brokerlink.LinkError, vault.VaultError) as exc:
            return {"status": "error", "error": str(exc)}

        spec = brokerlink.BROKERS[name]
        return {
            "status": "link_created",
            "broker": name,
            "label": spec["label"],
            "url": f"{public_url()}/link{request.query}",
            "expires_in_minutes": brokerlink.LINK_TTL_MINUTES,
            "next_step": f"Give the user this URL and ask them to open it and enter "
                         f"their {spec['label']} API keys there. Do not ask for the "
                         f"keys yourself. The link is single-use and expires in "
                         f"{brokerlink.LINK_TTL_MINUTES} minutes.",
        }

    @mcp.tool()
    def get_broker_status() -> dict[str, Any]:
        """Report which broker is linked and whether live trading is on.

        Shows the linked broker, live activation, kill-switch state, and the
        per-order and daily limits with how much of each has been used today.
        Reveals nothing sensitive. Call before any live-trading question.
        """
        if not hosted():
            return {**LOCAL_ONLY, "mode": "paper"}
        ctx = current_context()
        if (missing := require_account(ctx)) is not None:
            return missing
        with quiet_stdout():
            link = brokerlink.active_link(ctx)
            gates = limits.account_limits(ctx)
        return {
            "status": "ok",
            "mode": ctx.trading_mode.value,
            "is_real_money": ctx.is_live,
            "linked_broker": link,
            **gates,
            "next_step": (
                "No broker linked — use link_broker to connect one." if link is None
                else "Linked, but still in paper mode. Use switch_trading_mode('live') "
                     "to enable real orders."
                if not gates["live_activated"]
                else "Live trading is active. Real orders will be placed."
            ),
        }

    @mcp.tool()
    def unlink_broker(broker: str = "") -> dict[str, Any]:
        """Disconnect a broker and permanently delete its stored credentials.

        Also returns the account to paper mode, since there is nothing left to
        trade through. Leave `broker` empty to remove every linked account.
        """
        if not hosted():
            return LOCAL_ONLY
        ctx = current_context()
        if (missing := require_account(ctx)) is not None:
            return missing
        try:
            name = choice(broker, "broker", tuple(brokerlink.BROKERS), default="")
        except ToolInputError as exc:
            return {"status": "error", "error": str(exc)}

        with quiet_stdout():
            removed = brokerlink.unlink(ctx, name or None)
            registry.forget(ctx, name or None)
            if removed and brokerlink.active_link(ctx) is None:
                limits.deactivate_live(ctx)
        return {
            "status": "unlinked" if removed else "nothing_to_unlink",
            "removed": removed,
            "message": "Credentials deleted. The account is back in paper mode."
                       if removed else "No matching broker link was found.",
        }

    # ----------------------------------------------------------------- #
    # live trading
    # ----------------------------------------------------------------- #
    @mcp.tool()
    def switch_trading_mode(mode: str, confirm: str = "") -> dict[str, Any]:
        """Switch between paper and real-money trading.

        `mode` is "paper" or "live".

        Switching to **paper** is immediate — it is the safe direction, so it
        never needs confirmation. Real credentials stay linked, and the paper
        portfolio is exactly as the user left it.

        Switching to **live** puts real money at risk, so it requires:
          1. A linked broker (see link_broker).
          2. Telling the user plainly that real orders will be placed with their
             own money.
          3. The user typing exactly: I UNDERSTAND
          4. Passing that back as `confirm`.

        Never supply the confirmation phrase yourself, and never switch to live
        because the user linked a broker or asked a question about live trading —
        only because they asked for this switch.

        The two modes keep separate books: paper holdings are simulated, live
        holdings are whatever is really in the broker account. Portfolio and
        performance tools follow whichever mode is active, so say which one the
        user is looking at.
        """
        if not hosted():
            return LOCAL_ONLY
        ctx = current_context()
        if (missing := require_account(ctx)) is not None:
            return missing
        try:
            target = choice(mode, "mode", ("paper", "live"))
        except ToolInputError as exc:
            return {"status": "error", "error": str(exc)}

        with quiet_stdout():
            current = limits.account_limits(ctx)

        if target == "paper":
            if not current["live_activated"]:
                return {"status": "already_paper", "mode": "paper",
                        "message": "Already in paper mode — no real orders can be placed."}
            with quiet_stdout():
                limits.deactivate_live(ctx)
                link = brokerlink.active_link(ctx)
            return {
                "status": "switched", "mode": "paper", "is_real_money": False,
                "linked_broker": link,
                "message": "Switched to paper trading. Your broker is still linked, "
                           "so switching back is one step. You are now looking at "
                           "your simulated portfolio, not your real holdings.",
            }

        # --- going live ---
        if current["live_activated"]:
            return {"status": "already_live", "mode": "live", "is_real_money": True,
                    **current, "message": "Already trading live with real money."}

        with quiet_stdout():
            link = brokerlink.active_link(ctx)
        if link is None:
            return {
                "status": "no_broker_linked",
                "message": "No broker is linked, so there is nothing to trade through.",
                "next_step": "Call link_broker('groww') or link_broker('zerodha') "
                             "first, then switch to live.",
            }

        if (confirm or "").strip().upper() != "I UNDERSTAND":
            return {
                "status": "confirmation_required",
                "broker": link["label"],
                "message": f"Switching to live means real orders on the user's actual "
                           f"{link['label']} account, with their own money.",
                "next_step": "Tell the user plainly what this means, ask them to type "
                             "exactly 'I UNDERSTAND', then call this tool again with "
                             "that as `confirm`. Do not type it on their behalf.",
            }

        try:
            with quiet_stdout():
                limits.activate_live(ctx)
                gates = limits.account_limits(ctx)
        except limits.LimitExceeded as exc:
            return {"status": "error", "error": str(exc)}

        return {
            "status": "switched", "mode": "live", "is_real_money": True,
            "broker": link["label"], **gates,
            "message": "LIVE TRADING IS ON. Orders now use real money on the user's "
                       "own broker account. Every order still needs their explicit "
                       "confirmation, the per-order and daily caps still apply, and "
                       "set_kill_switch stops everything instantly. Switch back any "
                       "time with switch_trading_mode('paper').",
        }

    @mcp.tool()
    def set_kill_switch(enabled: bool = True) -> dict[str, Any]:
        """Stop or resume live trading immediately.

        With the kill switch on, every live order is refused until it is turned
        off — useful if something looks wrong, or the user simply wants to be
        certain nothing can trade. Takes effect on the very next order. Paper
        trading is unaffected.
        """
        if not hosted():
            return LOCAL_ONLY
        ctx = current_context()
        if (missing := require_account(ctx)) is not None:
            return missing
        try:
            with quiet_stdout():
                result = limits.set_kill_switch(ctx, bool(enabled))
        except limits.LimitExceeded as exc:
            return {"status": "error", "error": str(exc)}
        return {
            "status": "ok", **result,
            "message": "Kill switch ON — live orders are blocked until you turn it off."
                       if enabled else "Kill switch off. Live orders are allowed again.",
        }

    @mcp.tool()
    def set_daily_limits(max_daily_value: float = 0, max_daily_orders: int = 0) -> dict[str, Any]:
        """Set how much this account may trade in a single day.

        `max_daily_value` is the total rupee notional across live orders;
        `max_daily_orders` is how many live orders. Pass 0 to leave one unchanged.
        These sit on top of the per-order cap and cannot be bypassed by any tool.
        """
        if not hosted():
            return LOCAL_ONLY
        ctx = current_context()
        if (missing := require_account(ctx)) is not None:
            return missing
        try:
            value = non_negative_float(max_daily_value, "max_daily_value")
            count = int(max_daily_orders or 0)
            if count < 0:
                raise ToolInputError("max_daily_orders cannot be negative.")
        except ToolInputError as exc:
            return {"status": "error", "error": str(exc)}
        try:
            with quiet_stdout():
                gates = limits.set_daily_caps(
                    ctx, notional=value or None, orders=count or None
                )
        except limits.LimitExceeded as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "ok", **gates}
