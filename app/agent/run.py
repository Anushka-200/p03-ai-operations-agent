"""Entry points: run one customer turn, or resume a conversation after the owner decided an approval.

One conversation = one LangGraph thread, keyed by the customer's keyed phone hash (never the raw number).
"""

import logging
from collections.abc import Callable
from typing import Any

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row

from app import llm as llm_module
from app.agent.graph import approval_note, build_graph
from app.settings import get_settings
from app.tools import Caller, ToolContext

logger = logging.getLogger(__name__)


def setup_checkpointer() -> None:
    """Create LangGraph's checkpoint tables. Needs the migrator role (DDL); the app role only reads/writes."""
    with psycopg.connect(get_settings().migrator_database_url, autocommit=True, row_factory=dict_row) as conn:
        PostgresSaver(conn).setup()


class Agent:
    def __init__(self, graph, llm: Callable[..., Any] = llm_module.complete) -> None:
        self.graph, self.llm = graph, llm

    def _config(self, ctx: ToolContext, caller: Caller) -> dict[str, Any]:
        row = ctx.conn.execute(
            "SELECT phone_hash FROM customers WHERE id=%s", (caller.customer_id,)
        ).fetchone()
        assert row is not None, "the caller's customer must exist"
        return {"configurable": {"thread_id": row[0], "ctx": ctx, "caller": caller, "llm": self.llm}}

    def run_turn(self, ctx: ToolContext, caller: Caller, inbound_id: int, text: str) -> str | None:
        config = self._config(ctx, caller)
        # Reset the per-message fields; the checkpoint carries everything per-conversation.
        turn = {
            "messages": [{"role": "user", "content": text}],
            "turn_tool_calls": 0, "reply": None, "draft": None, "pending_approvals": [], "handoff_reason": None,
        }  # fmt: skip
        state = self.graph.invoke(turn, config)
        return state.get("reply")

    def resume_after_approval(
        self, ctx: ToolContext, customer_id: int, decision: dict[str, Any]
    ) -> str | None:
        """The owner decided. Continue the paused thread; returns the message for the customer."""
        caller = Caller("customer", customer_id)
        config = self._config(ctx, caller)
        if self.graph.get_state(config).next:  # still paused at approval_wait: the normal resume
            return self.graph.invoke(Command(resume=decision), config).get("reply")
        # The customer wrote again while waiting, which dropped the pause (LangGraph starts a fresh
        # run for a new message). The decision must not be lost, so deliver it as a new turn.
        turn = {
            "messages": [{"role": "user", "content": approval_note(decision)}],
            "turn_tool_calls": 0, "reply": None, "draft": None, "pending_approvals": [], "handoff_reason": None,
        }  # fmt: skip
        return self.graph.invoke(turn, config).get("reply")

    def is_waiting_for_approval(self, ctx: ToolContext, customer_id: int) -> bool:
        snap = self.graph.get_state(self._config(ctx, Caller("customer", customer_id)))
        return bool(snap.next)


_default: Agent | None = None


def default_agent() -> Agent:
    """The process-wide agent on the real checkpointer. Created lazily so imports stay cheap."""
    global _default
    if _default is None:
        conn = psycopg.connect(get_settings().database_url, autocommit=True, row_factory=dict_row)
        _default = Agent(build_graph(PostgresSaver(conn)))
    return _default


def run_turn(ctx: ToolContext, caller: Caller, inbound_id: int, text: str) -> str | None:
    return default_agent().run_turn(ctx, caller, inbound_id, text)
