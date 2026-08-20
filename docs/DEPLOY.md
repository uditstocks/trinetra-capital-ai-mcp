# Deploying Trinetra (Railway)

The hosted server does three things the local one does not: it serves many users
over HTTPS, it authenticates them, and it holds their broker credentials. Each of
those needs configuration, and the server **refuses to start** if the dangerous
combinations are missing — a database with no authentication would expose every
account, so that is a startup failure rather than something to find in production.

---

## 1. Generate the vault master key

This key encrypts every user's broker credentials. Generate it **once**, keep a
copy somewhere safe, and never commit it.

```bash
python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"
```

> Lose this key and every stored credential becomes unreadable — users would have
> to re-link their brokers. Leak it *together with a database dump* and their
> broker keys are exposed. Treat it like a production database password.

---

## 2. Sign-in with Google (Auth0 free tier)

Users sign in with Google. Auth0 is the machinery that issues the tokens — your
users never see it as a separate account, they see a Google button.

1. Create an **API**: Applications → APIs → Create.
   - Identifier: `https://api.trinetra.app` → this is `OAUTH_AUDIENCE`
   - Signing algorithm: RS256
2. **Authentication → Social → Google**. Toggle it on.
   - Auth0's shared dev keys work for testing but rate-limit and show Auth0's name
     on the consent screen. Before launch, create your own credentials in Google
     Cloud Console (OAuth client, type *Web application*) and paste the client id
     and secret here — otherwise users see someone else's app name when they sign in.
   - Authorised redirect URI in Google Cloud:
     `https://<your-tenant>.auth0.com/login/callback`
3. **Turn off** Database → Username-Password-Authentication if you want Google to
   be the only way in. Otherwise users can create email/password accounts too.
4. `OAUTH_ISSUER` is `https://<your-tenant>.<region>.auth0.com/` — the trailing
   slash matters.
5. **Enable Dynamic Client Registration**: Settings → Advanced → *OIDC Dynamic
   Application Registration*. MCP hosts register themselves through it. Without
   this, Claude cannot complete the connection at all — this is the single most
   common reason a remote MCP server fails to connect.
6. Settings → Advanced → **Default Audience**: set it to your API identifier, so
   tokens are issued for the right resource.

### What the user actually experiences

```
Claude: POST /mcp with no token   →  401 + pointer to our metadata
Claude: GET  /.well-known/oauth-protected-resource  →  "the AS is Auth0"
Claude: registers itself with Auth0, opens a browser
User:   clicks "Sign in with Google", picks their account
Claude: gets a token, connects. Account auto-provisioned on first login.
```

No API key, no password, nothing typed into the chat.

---

## 3. Railway

New Project → Deploy from GitHub repo → add a **PostgreSQL** service.

Set these in the service's **Variables**:

| Variable | Value |
|---|---|
| `TRINETRA_TRANSPORT` | `http` |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `PUBLIC_URL` | `https://<your-app>.up.railway.app` |
| `OAUTH_ISSUER` | `https://<tenant>.auth0.com/` |
| `OAUTH_AUDIENCE` | `https://api.trinetra.app` |
| `TRINETRA_MASTER_KEY` | the key from step 1 |
| `GROWW_MAX_ORDER_VALUE` | e.g. `25000` — start small |

The Dockerfile runs `alembic upgrade head` before starting, so a failed migration
fails the release instead of booting against a stale schema.

### Zerodha users need one extra step

Kite ties its redirect to the API key's own app, so each user sets their Kite
app's **Redirect URL** to:

```
https://<your-app>.up.railway.app/link/zerodha/callback
```

Groww needs nothing equivalent — it is a straight key exchange.

---

## 4. Verify before telling anyone

```bash
curl https://<your-app>.up.railway.app/health
# {"server":"ok","auth":"on","database":"ok"}
```

`"auth":"off"` with a database configured means the server should not have
started — check the logs. `"database":"unreachable"` returns HTTP 503.

Then connect from Claude (Settings → Connectors → Add custom connector):

```
https://<your-app>.up.railway.app/mcp
```

Walk the whole path once yourself:

- [ ] Log in via Google, `get_account_status` returns `ready`
- [ ] `view_portfolio` on a second account shows *its own* data, not yours
- [ ] `link_broker('groww')` returns a URL; the page loads over HTTPS
- [ ] After linking, `get_broker_status` shows the broker and **paper** mode
- [ ] `switch_trading_mode('live')` without the phrase is refused
- [ ] With `I UNDERSTAND`, mode becomes live
- [ ] One real order of **one share**, verified in the broker's own app
- [ ] `set_kill_switch(True)` → the next live order is refused
- [ ] `switch_trading_mode('paper')` → back to simulated

---

## Operating it

| Situation | What to do |
|---|---|
| Something looks wrong, stop all live trading now | Set `TRINETRA_DISABLE_LIVE=1` and redeploy. Paper keeps working. |
| Rotate the master key | Add `TRINETRA_MASTER_KEYS=v1:<old>,v2:<new>` and `TRINETRA_ACTIVE_KEY=v2`, then run `brokerlink.rotate_all()`. Old records stay readable throughout. |
| A user reports a bad order | Every attempt is in `audit_events`, hash-chained, keyed by account and time. |
| Roll back | Railway → Deployments → Redeploy the previous build. Check whether the newer release migrated the schema first. |

---

## Known limits at launch

Worth knowing before you tell users otherwise:

- **Kite sessions expire daily.** Zerodha users re-link each trading day. This is
  Kite's design, not something we can refresh around.
- **Booked-P&L analytics are paper-only.** Neither broker exposes historical
  realised P&L for delivery holdings, so live mode reports what the broker
  reports rather than inventing a figure.
- **No reconciliation job yet.** If an order's status changes broker-side after we
  record it, our copy can go stale until it is read again.
- **Single region.** Fine for launch; latency to NSE is not on the critical path
  because orders are user-initiated.
