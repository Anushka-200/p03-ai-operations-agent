# ADR 004: MCP boundary

Status: accepted (2026-10-09), E6 measures the in-process vs MCP cost

## Context
The same tools should be usable by our agent, by the owner from Claude Desktop, and later by P11's voice bot.
Permissions must hold for every caller, including ones we do not control.

## Options
- Separate tool code per caller: duplicated logic, drifting fixes.
- Agent always calls tools through MCP: one path, but an extra process and latency on every tool call.
- **One registry + gate; MCP server wraps it; agent calls in-process by default, MCP behind a flag.**

## Decision
- `app/tools/registry.py` holds typed tools; `app/gate.py` checks every call; `app/mcp_server.py` (official Python
  SDK, FastMCP) exposes the registry. The gate sits below MCP, so Claude Desktop cannot bypass it.
- Transport: stdio only in v1 (Claude Desktop on Windows launches it through `wsl.exe`); role `owner`; dry-run.
- No streamable-HTTP transport or public endpoint without the owner's approval (it would need OAuth).
- `TOOLS_VIA_MCP=true` runs the agent through an MCP client; experiment E6 checks identical results and the
  latency added.
- Price table and FAQ also exposed as MCP resources for browsing in Claude Desktop; the agent uses the narrow tools.

## Consequences
- One permission and audit story for every caller.
- Sampling and elicitation (server asking the client's model or user) are not used in v1.
