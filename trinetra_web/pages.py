"""HTML for the broker-linking pages.

Self-contained: no CDN, no external fonts, no analytics. This page handles
brokerage credentials, so nothing on it may be fetched from a third party.

The copy is deliberately plain about what is being asked for and what happens to
it — a page that asks for API keys and explains nothing is indistinguishable from
a phishing page.
"""

from __future__ import annotations

from html import escape
from typing import Any

_STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2.5rem 1.25rem; background: #f5f5f4; color: #0b0b0b;
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.card {
  max-width: 34rem; margin: 0 auto; background: #fff; border: 1px solid #e6e5e1;
  border-radius: 14px; padding: 2rem; box-shadow: 0 1px 3px rgba(0,0,0,.05);
}
h1 { font-size: 1.3rem; margin: 0 0 .35rem; letter-spacing: -.01em; }
.sub { color: #52514e; margin: 0 0 1.5rem; font-size: .93rem; }
label { display: block; font-weight: 600; margin: 1.1rem 0 .35rem; font-size: .9rem; }
.hint { font-weight: 400; color: #52514e; font-size: .82rem; margin: .2rem 0 0; }
input[type=text], input[type=password] {
  width: 100%; padding: .65rem .75rem; border: 1px solid #d6d5d1; border-radius: 8px;
  font-size: .95rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: #fcfcfb;
}
input:focus { outline: 2px solid #2a78d6; outline-offset: 1px; border-color: #2a78d6; }
button {
  margin-top: 1.6rem; width: 100%; padding: .8rem; border: 0; border-radius: 8px;
  background: #2a78d6; color: #fff; font-size: .98rem; font-weight: 600; cursor: pointer;
}
button:hover { background: #256abf; }
.note {
  margin-top: 1.5rem; padding: .85rem 1rem; background: #f7f9fc; border: 1px solid #dce9f9;
  border-radius: 8px; font-size: .85rem; color: #34527a;
}
.note ul { margin: .5rem 0 0; padding-left: 1.1rem; }
.note li { margin: .25rem 0; }
.status { text-align: center; padding: 1rem 0; }
.status .mark { font-size: 2.6rem; line-height: 1; }
.ok { color: #0ca30c; } .bad { color: #d03b3b; }
.muted { color: #52514e; font-size: .9rem; margin-top: .75rem; }
code { background: #f0efec; padding: .1rem .3rem; border-radius: 4px; font-size: .85em; }
"""


def _shell(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='referrer' content='no-referrer'>"
        f"<title>{escape(title)} · Trinetra</title><style>{_STYLE}</style></head>"
        f"<body><div class='card'>{body}</div></body></html>"
    )


def link_form(broker: str, spec: dict[str, Any], action: str) -> str:
    """The credential form. Inputs are password-type so keys are not shoulder-read
    or captured by a screen recorder, and autocomplete is off throughout."""
    fields = []
    for field in spec["fields"]:
        name = escape(field["name"])
        required = " required" if field.get("required") else ""
        optional = "" if field.get("required") else " <span class='hint'>(optional)</span>"
        hint = f"<p class='hint'>{escape(field['help'])}</p>" if field.get("help") else ""
        fields.append(
            f"<label for='{name}'>{escape(field['label'])}{optional}</label>"
            f"<input type='password' id='{name}' name='{name}'{required} "
            f"autocomplete='off' spellcheck='false' autocapitalize='off'>{hint}"
        )

    return _shell(
        f"Connect {spec['label']}",
        f"<h1>Connect your {escape(spec['label'])} account</h1>"
        f"<p class='sub'>Trinetra will be able to place orders you approve, and to "
        f"read your holdings and funds.</p>"
        f"<form method='post' action='{escape(action)}' autocomplete='off'>"
        + "".join(fields)
        + "<button type='submit'>Connect securely</button></form>"
        "<div class='note'><strong>What happens to these keys</strong><ul>"
        "<li>They are encrypted before they are stored, with a key held outside "
        "the database.</li>"
        "<li>They are never shown in your AI chat, never written to logs, and never "
        "returned by any tool.</li>"
        "<li>Trading stays in paper mode until you switch live on separately.</li>"
        "<li>You can remove them at any time by asking Trinetra to unlink.</li>"
        "</ul></div>"
        f"<p class='muted'>Get your keys from "
        f"<code>{escape(spec.get('help_url', ''))}</code></p>",
    )


def result_page(ok: bool, heading: str, detail: str) -> str:
    mark = "&#10003;" if ok else "&#10005;"
    tone = "ok" if ok else "bad"
    tail = ("<p class='muted'>You can close this tab and go back to your AI.</p>"
            if ok else
            "<p class='muted'>Ask Trinetra for a fresh link and try again.</p>")
    return _shell(
        heading,
        f"<div class='status'><div class='mark {tone}'>{mark}</div>"
        f"<h1>{escape(heading)}</h1><p class='sub'>{escape(detail)}</p>{tail}</div>",
    )
