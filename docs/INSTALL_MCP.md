# Connect Trinetra to your AI

Five minutes. **No API keys, no accounts, no deployment** — the server runs on
your own machine and your AI talks to it directly.

---

## 1. Install

```bash
git clone <this-repo>
cd Trinetra-Capital-AI
pip install -r requirements-mcp.txt
```

Needs Python 3.10+. `requirements-mcp.txt` is the lean set — just the MCP
protocol, market data and the analysis maths. (`requirements.txt` is the bigger
list, only needed for the older interactive CLI.)

Check it starts, then press `Ctrl+C`:

```bash
python -m trinetra_mcp
```

Silence plus a log line is correct — it is waiting to be spoken to over stdio.

---

## 2. Point your AI at it

### Claude Desktop

Edit the config file (create it if missing):

| OS | Path |
|---|---|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "trinetra": {
      "command": "python",
      "args": ["-m", "trinetra_mcp"],
      "env": {
        "PYTHONPATH": "F:\\Trinetra-Capital-AI MCP"
      }
    }
  }
}
```

Set `PYTHONPATH` to wherever you cloned the repo. On Windows use double
backslashes. **Restart Claude Desktop completely** — quit it, don't just close
the window. Trinetra's tools then appear in the tools menu.

If `python` is not found, use the full interpreter path instead — get it with
`python -c "import sys; print(sys.executable)"`.

### ChatGPT Desktop

Add the same server under Settings → Connectors → MCP. It takes the same
command, args and env. MCP support there is newer than Claude's, so if something
behaves oddly, check it against Claude Desktop first.

### Claude Code

```bash
claude mcp add trinetra -- python -m trinetra_mcp
```

Run it from the repo directory, or add `-e PYTHONPATH=<repo path>`.

---

## 3. Use it

Just talk normally:

```
set me up for paper trading
what's the price of Reliance?
how does Infosys look right now?
buy 10 shares of HCL
show my portfolio
how much profit have I booked?
```

The first message creates a paper account with ₹1,00,000 in virtual cash.
**Prices and analysis are real and live — only the money is simulated.**

Buying takes two steps on purpose: your AI shows you a preview (symbol, quantity,
price, total) and places nothing until you say yes.

---

## Where your data lives

`~/.trinetra/users/local/` — portfolio, account record and an order audit log.
Nothing leaves your machine. Override the location with `TRINETRA_DATA_DIR`.

Already used the old CLI and want that paper history? Set
`TRINETRA_IMPORT_LOCAL_PORTFOLIO=1` before first setup. It is off by default so a
fresh copy of the repo never hands you someone else's positions.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Tools don't appear | Fully quit and reopen the app. Confirm the JSON is valid (a trailing comma breaks it silently). |
| "No module named trinetra_mcp" | `PYTHONPATH` is wrong. It must be the folder *containing* `trinetra_mcp/`. |
| "python not found" | Use the absolute interpreter path as `command`. |
| Prices missing for a stock | Market data is live; some symbols have thin data outside market hours. |
| Everything is slow on first use | The exchange instrument list downloads once (a few MB), then caches for a day. |

Claude Desktop logs: `%APPDATA%\Claude\logs\` (Windows),
`~/Library/Logs/Claude/` (macOS).

---

## Sending it to a friend

Repo link + these two lines:

```bash
pip install -r requirements-mcp.txt
```
…then the config block above with their own path. No keys to share, no account
to create, no server to deploy. Their data stays on their machine.
