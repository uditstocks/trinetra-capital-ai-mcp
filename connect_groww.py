#!/usr/bin/env python
"""
🔱  Connect your Groww account to Trinetra Capital AI.

This is the one-stop setup + health check. It will:
  1. Walk you through getting Groww API credentials (if you don't have them).
  2. Authenticate and cache a daily access token.
  3. Verify the connection by reading your profile, funds and holdings.

Usage:
    python connect_groww.py

Nothing here places an order — it is read-only and safe to run anytime to
confirm the connection is healthy.
"""

from __future__ import annotations

import sys

from trinetra.config import AuthMethod, settings


SETUP_GUIDE = """
────────────────────────────────────────────────────────────────────────
 HOW TO GET YOUR GROWW API CREDENTIALS
────────────────────────────────────────────────────────────────────────
 1. Open  https://groww.in/trade-api/docs  and log in to your Groww account.
 2. Go to the "Groww Cloud / API Keys" page and click "Generate API Key".
 3. Choose ONE of the two flows:

    A) TOTP flow  (recommended — fully automated, token rotates daily)
       • Click "Generate TOTP Token".
       • Copy the TOTP Token   -> put it in GROWW_API_KEY
       • Copy the TOTP Secret  -> put it in GROWW_TOTP_SECRET

    B) Approval / API-key+secret flow
       • Copy the API Key      -> put it in GROWW_API_KEY
       • Copy the API Secret   -> put it in GROWW_API_SECRET

 4. Add the values to your .env file in this folder, for example:

       GROWW_API_KEY=your_totp_token_or_api_key
       GROWW_TOTP_SECRET=your_totp_secret        # for flow A
       # GROWW_API_SECRET=your_api_secret         # for flow B

 5. (Optional) Keep paper trading on while you test:
       GROWW_TRADING_MODE=paper      # default; switch to 'live' for real orders

 6. Re-run:  python connect_groww.py
────────────────────────────────────────────────────────────────────────
"""


def main() -> int:
    print("\n🔱  Trinetra ↔ Groww connection setup\n" + "─" * 60)

    if not settings.groww_configured:
        print("❌ No Groww credentials found in your environment / .env.")
        print(SETUP_GUIDE)
        return 1

    method = "TOTP" if settings.auth_method is AuthMethod.TOTP else "API key + secret"
    print(f"✓ Credentials detected (auth method: {method}).")
    print(f"  Trading mode: {settings.trading_mode.value.upper()}")
    print("  Authenticating with Groww…")

    # Import here so a missing SDK produces a clean message via BrokerError.
    from trinetra.broker import groww_client
    from trinetra.broker.base import BrokerError

    try:
        client = groww_client.get_client(force_refresh=True)
    except BrokerError as exc:
        print(f"\n❌ {exc}")
        print(SETUP_GUIDE)
        return 1

    print("✓ Authenticated. Access token cached for today.\n")

    ok = True

    # --- profile ---
    try:
        profile = client.get_user_profile()
        print("👤 Account")
        print(f"   UCC: {profile.get('ucc')}")
        print(f"   NSE enabled: {profile.get('nse_enabled')}  "
              f"BSE enabled: {profile.get('bse_enabled')}")
        print(f"   Active segments: {profile.get('active_segments')}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"   ⚠️ Could not read profile: {exc}")

    # --- funds ---
    try:
        margin = client.get_available_margin_details()
        eq = margin.get("equity_margin_details", {}) or {}
        print("\n💰 Funds")
        print(f"   Clear cash: ₹{margin.get('clear_cash')}")
        print(f"   CNC available: ₹{eq.get('cnc_balance_available')}  "
              f"MIS available: ₹{eq.get('mis_balance_available')}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n   ⚠️ Could not read funds: {exc}")

    # --- holdings ---
    try:
        holdings = client.get_holdings_for_user().get("holdings", [])
        print(f"\n📦 Holdings: {len(holdings)} instrument(s)")
        for h in holdings[:10]:
            print(f"   • {h.get('trading_symbol')}: "
                  f"{h.get('quantity')} @ ₹{h.get('average_price')}")
        if len(holdings) > 10:
            print(f"   … and {len(holdings) - 10} more")
    except Exception as exc:  # noqa: BLE001
        print(f"\n   ⚠️ Could not read holdings: {exc}")

    print("\n" + "─" * 60)
    if ok:
        print("✅ Groww is connected and healthy.")
        if settings.is_live:
            print("   You are in LIVE mode — agent orders will use real money.")
        else:
            print("   You are in PAPER mode — flip GROWW_TRADING_MODE=live for real orders.")
        print("   Start the system with:  python main.py")
    else:
        print("⚠️ Connected, but some reads failed (check API permissions/segments).")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
