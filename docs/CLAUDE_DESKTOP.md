# Using the tools from Claude Desktop (owner view)

The MCP server runs as a subprocess that Claude Desktop starts. It talks over stdin/stdout, so it opens
**no network port**. Claude Desktop runs on Windows and the project lives in WSL, so the command goes
through `wsl.exe`.

## Setup
1. Start the database: in WSL, `cd ~/portfolio/p03-ai-operations-agent && docker compose up -d`.
2. In Claude Desktop open **Settings → Developer → Edit Config**. It opens
   `%APPDATA%\Claude\claude_desktop_config.json`. Add this inside the top-level object (keep any
   other servers you already have):

```json
{
  "mcpServers": {
    "coolbreeze-ops": {
      "command": "wsl.exe",
      "args": [
        "-d", "Ubuntu",
        "--cd", "/home/<you>/portfolio/p03-ai-operations-agent",
        "--exec", "/home/<you>/.local/bin/uv",
        "run", "python", "-m", "app.mcp_server"
      ]
    }
  }
}
```
3. Quit Claude Desktop completely (system tray → Quit) and start it again.
4. A tools icon appears in the chat box. Open it: you should see `coolbreeze-ops` with 13 tools.

## Safe by design
- It runs in **dry-run** (`DRY_RUN=true` in `.env`): messages are recorded, never sent.
- You are the **owner** role: you can read leads/jobs/prices, but you cannot book on a customer's behalf.
- Anything that sends messages or moves money creates an **approval request**. There is deliberately no
  "approve" tool, so the model cannot approve its own request; approval happens in Telegram (Phase 3).
- Every call, allowed or refused, is written to the `audit_log` table.

## Try these prompts
- "What do we charge for an AC tune-up?"  → uses `get_price`
- "Which leads from the last 3 days haven't booked?"  → `list_leads`
- "What jobs do we have on Thursday?"  → `list_jobs`
- "Draft a follow-up text to everyone who hasn't booked, then send it."  → a draft, then
  `needs_approval`; nothing is sent.
- "Give customer 1 a 90% discount."  → refused by code (cap is 15%).

## If it does not appear
- Logs: Claude Desktop → Settings → Developer → open the log for `coolbreeze-ops`.
- Run the same command in a Windows terminal: `wsl.exe -d Ubuntu --cd /home/<you>/portfolio/p03-ai-operations-agent --exec /home/<you>/.local/bin/uv run python -m app.mcp_server`. It should wait silently (it is waiting for a client). Press Ctrl+C.
- Check the distro name with `wsl.exe -l -q` (the config uses `Ubuntu`).
- Postgres must be running on port 5435.
