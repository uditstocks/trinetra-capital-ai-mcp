"""The page someone lands on when they visit the domain.

Two audiences arrive here: a person deciding whether to connect Trinetra, and a
person who already has and wants to know what it can do. Both need to see the
safety model before anything else — a page that asks you to wire an AI to your
brokerage account and does not explain the guardrails has not earned the click.

Self-contained by the same rule as the linking page: no CDN, no fonts, no
analytics on a domain that also handles credentials.
"""

from __future__ import annotations

import os

from starlette.responses import HTMLResponse
from starlette.routing import Route

_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #f5f5f4; --card: #fff; --ink: #0b0b0b; --muted: #52514e;
  --line: #e6e5e1; --accent: #2a78d6; --good: #0ca30c; --code: #f0efec;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #171716; --card: #201f1e; --ink: #f5f5f4; --muted: #b3b1ab;
    --line: #33322f; --accent: #6da7ec; --good: #4ec24e; --code: #2a2927;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 3rem 1.25rem 4rem; background: var(--bg); color: var(--ink);
  font: 16px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 44rem; margin: 0 auto; }
.hero { text-align: center; margin-bottom: 2.5rem; }
h1 { font-size: 2rem; margin: 0 0 .5rem; letter-spacing: -.02em; }
.tag { color: var(--muted); font-size: 1.05rem; margin: 0; }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 14px;
  padding: 1.6rem 1.75rem; margin-bottom: 1.1rem;
}
h2 { font-size: 1.05rem; margin: 0 0 .9rem; letter-spacing: -.01em; }
p { margin: .5rem 0; }
.muted { color: var(--muted); font-size: .92rem; }
code, pre {
  background: var(--code); border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .88rem;
}
code { padding: .15rem .4rem; }
pre { padding: .85rem 1rem; overflow-x: auto; margin: .75rem 0 0; }
ol, ul { margin: .6rem 0; padding-left: 1.3rem; }
li { margin: .4rem 0; }
.guards { list-style: none; padding: 0; }
.guards li { display: flex; gap: .65rem; align-items: flex-start; margin: .7rem 0; }
.guards .tick { color: var(--good); font-weight: 700; flex: none; }
.steps { counter-reset: s; list-style: none; padding: 0; }
.steps li { counter-increment: s; position: relative; padding-left: 2.1rem; margin: .9rem 0; }
.steps li::before {
  content: counter(s); position: absolute; left: 0; top: .05rem;
  width: 1.5rem; height: 1.5rem; border-radius: 50%; background: var(--accent);
  color: #fff; font-size: .8rem; font-weight: 700; display: grid; place-items: center;
}
footer { text-align: center; color: var(--muted); font-size: .85rem; margin-top: 2rem; }
.warn {
  border-left: 3px solid #d03b3b; padding-left: .9rem; margin-top: 1rem;
  color: var(--muted); font-size: .9rem;
}
"""


def _page(mcp_url: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trinetra Capital AI</title><style>{_STYLE}</style></head><body><main>

<div class="hero">
  <h1>Trinetra Capital AI</h1>
  <p class="tag">Research and trade Indian equities by talking to the AI you already use.</p>
</div>

<div class="card">
  <h2>Connect it</h2>
  <ol class="steps">
    <li>In Claude, open <strong>Settings &rarr; Connectors &rarr; Add custom connector</strong>
        and paste this URL:
      <pre>{mcp_url}</pre></li>
    <li>Sign in with <strong>Google</strong>. A browser window opens — you never type
        a password or an API key into the chat.</li>
    <li>Say <em>&ldquo;set me up&rdquo;</em>. You get a paper account with virtual cash,
        trading against real live market prices.</li>
  </ol>
  <p class="muted">Works with any MCP-capable AI host. Claude is the one we test against.</p>
</div>

<div class="card">
  <h2>What you can ask</h2>
  <ul>
    <li>&ldquo;How does Infosys look right now?&rdquo; &mdash; full indicator analysis with
        the reasoning shown, not just a verdict</li>
    <li>&ldquo;Buy 10 shares of HCL&rdquo; &mdash; you get a preview and approve it before
        anything is placed</li>
    <li>&ldquo;Show my portfolio&rdquo; &mdash; holdings, allocation and P&amp;L, with a chart</li>
    <li>&ldquo;How much profit have I booked?&rdquo; &mdash; realised P&amp;L, win rate,
        best and worst trades</li>
  </ul>
</div>

<div class="card">
  <h2>Before you connect a real account</h2>
  <ul class="guards">
    <li><span class="tick">&#10003;</span><span><strong>Paper by default.</strong>
      Connecting a broker does not enable real money. Going live is a separate step
      where you type <code>I UNDERSTAND</code> yourself.</span></li>
    <li><span class="tick">&#10003;</span><span><strong>Two steps for every order.</strong>
      The AI can prepare an order; only your explicit confirmation places it.</span></li>
    <li><span class="tick">&#10003;</span><span><strong>Your keys are never typed in chat.</strong>
      They are entered on a page here over HTTPS, encrypted before storage, and never
      returned by any tool or written to any log.</span></li>
    <li><span class="tick">&#10003;</span><span><strong>Limits you set.</strong>
      A per-order cap, a daily value cap and a daily order count &mdash; all enforced
      on the server, none of them something the AI can talk its way past.</span></li>
    <li><span class="tick">&#10003;</span><span><strong>A kill switch.</strong>
      Stops live trading on the very next order. Paper keeps working.</span></li>
  </ul>
  <p class="warn"><strong>Risk.</strong> In live mode this places real orders with real
  money on your own broker account, and you are responsible for every one of them.
  Trading carries risk of loss. Nothing here is investment advice. Start in paper mode
  and keep your caps small.</p>
</div>

<footer>Built for NSE &amp; BSE &middot; Groww and Zerodha &middot;
<a href="/health" style="color:var(--muted)">status</a></footer>
</main></body></html>"""


async def landing(request):
    public = os.getenv("PUBLIC_URL", "").rstrip("/")
    return HTMLResponse(
        _page(f"{public}/mcp" if public else "/mcp"),
        headers={"Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"},
    )


ROUTES = [Route("/", landing, methods=["GET"])]
