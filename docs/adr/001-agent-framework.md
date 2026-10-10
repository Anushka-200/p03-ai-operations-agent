# ADR 001: Agent framework

Status: accepted (2026-10-09), revisit after Phase 2

## Context
The agent must hold a conversation per customer that can pause for days (a reply, an owner approval) and resume
exactly where it stopped, survive restarts, and run fixed code before and around the LLM (emergency router,
permission gate). P02 ADR 010 found plain Python better for a straight RAG pipeline; this problem is different
because pause/resume is core.

## Options
- **Plain Python loop** (~30 lines) + our own state table + our own pause/resume for approvals: fully visible;
  about 1-2 days more code and tests for checkpointing and interrupts.
- **LangGraph, tiny graph**: ready-made Postgres checkpointer and `interrupt()`/resume; nodes are plain functions.
  Adds a dependency and its API churn; graph shape could tempt us into a big flowchart.
- **Agent SDKs without graphs** (OpenAI Agents SDK, Pydantic AI, Claude Agent SDK): good loops, weaker
  durable pause/resume across days; Claude Agent SDK is aimed at coding-style agents.

## Decision
LangGraph with a deliberately small graph: `pre_router → compact_if_long → agent → gate → run_tools → agent`,
plus `interrupt` for approvals, `output_guard`, `send_reply`, `handoff`. The conversation steps (ask, quote, book)
stay inside the agent's judgment, not in edges. Tools and the gate are plain Python, independent of LangGraph, so
the MCP server and the scheduler use them without the graph.

## Consequences
- Checkpoints and interrupts come for free; a restart test proves resume works.
- If LangGraph gets in the way, only `app/agent/` changes; tools, gate and adapters do not.
- Upwork keyword covered honestly.
