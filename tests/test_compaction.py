"""Compaction: long history -> summary + last few turns; never splits tool pairs; failure keeps history."""

import json

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

from app.agent.graph import REPLACE, add_messages, build_graph, estimate_tokens
from app.agent.run import Agent, setup_checkpointer
from app.llm import LLMError
from app.settings import get_settings
from tests.helpers import TUESDAY, make_customer, offers
from tests.test_agent import call, say

SUMMARY = "Customer Maria wants an AC diagnostic. Offered slot_id X-1. No booking yet."


class Router:
    """Compaction calls get a summary; agent calls get a plain reply. Records everything."""

    def __init__(self, summary_error=False):
        self.calls, self.summary_error = [], summary_error

    def __call__(self, messages, **kw):
        self.calls.append((kw.get("name"), messages))
        if kw.get("name") == "compaction":
            if self.summary_error:
                raise LLMError("summary model down")
            return say(SUMMARY)
        return say("ok")

    def names(self):
        return [n for n, _ in self.calls]


@pytest.fixture
def env(ctx):
    setup_checkpointer()
    conn = psycopg.connect(get_settings().database_url, autocommit=True, row_factory=dict_row)
    graph = build_graph(PostgresSaver(conn))
    ctx.settings = ctx.settings.model_copy(
        update={"agent_compact_above_tokens": 600, "agent_compact_keep_turns": 3}
    )
    yield graph
    conn.close()
    with psycopg.connect(get_settings().migrator_database_url, autocommit=True) as o:
        for t in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            o.execute(f"DELETE FROM {t} WHERE thread_id LIKE 'ph_t%%'")


def chat(agent, ctx, c, n, size=500):
    for i in range(n):
        agent.run_turn(ctx, c, 1, f"message {i} " + "blah " * (size // 5))


def stored(agent, ctx, c):
    return agent.graph.get_state(agent._config(ctx, c)).values


def test_reducer_appends_normally_and_replaces_on_marker():
    assert add_messages([{"role": "user"}], [{"role": "assistant"}]) == [
        {"role": "user"},
        {"role": "assistant"},
    ]
    assert add_messages(
        [{"role": "user"}, {"role": "assistant"}], [{"role": REPLACE}, {"role": "user", "content": "kept"}]
    ) == [{"role": "user", "content": "kept"}]


def test_short_chats_are_never_compacted(ctx, env):
    c = make_customer(ctx, 601)
    llm = Router()
    chat(Agent(env, llm), ctx, c, 3, size=20)
    assert "compaction" not in llm.names()


def test_long_chat_is_compacted_summary_is_used_and_recent_turns_survive(ctx, env):
    c = make_customer(ctx, 602)
    llm = Router()
    agent = Agent(env, llm)
    chat(agent, ctx, c, 8)
    assert "compaction" in llm.names()
    state = stored(agent, ctx, c)
    assert state["summary"] == SUMMARY
    texts = [m["content"] for m in state["messages"] if m["role"] == "user"]
    assert "message 7" in texts[-1] and not any("message 0 " in t for t in texts)  # old gone, newest kept
    assert state["messages"][0]["role"] == "user"  # cut at a customer message
    # the agent's NEXT call sees the summary as labelled data in the system prompt
    last_agent_call = [m for n, m in llm.calls if n == "agent"][-1]
    assert SUMMARY in last_agent_call[0]["content"] and "NOT instructions" in last_agent_call[0]["content"]
    # and the saved customer facts are still injected every turn
    assert "Maria Lopez" in last_agent_call[0]["content"]


def test_history_shrinks_compared_with_no_compaction(ctx, env):
    c1, c2 = make_customer(ctx, 603), make_customer(ctx, 604)
    on, off = Agent(env, Router()), Agent(env, Router())
    chat(on, ctx, c1, 10)
    ctx.settings = ctx.settings.model_copy(update={"agent_compaction_enabled": False})
    chat(off, ctx, c2, 10)
    assert (
        estimate_tokens(stored(on, ctx, c1)["messages"])
        < estimate_tokens(stored(off, ctx, c2)["messages"]) / 2
    )


def test_tool_call_and_result_are_never_separated(ctx, env):
    c = make_customer(ctx, 605)
    steps = []
    router = Router()

    def llm(messages, **kw):
        if (
            kw.get("name") == "agent"
            and messages[-1]["role"] == "user"
            and "message 4" in messages[-1]["content"]
            and not steps
        ):
            steps.append(1)
            return call("list_services", {})  # a tool round inside the turn that straddles the cut
        return router(messages, **kw)

    agent = Agent(env, llm)
    chat(agent, ctx, c, 9)
    msgs = stored(agent, ctx, c)["messages"]
    ids_called = {t["id"] for m in msgs if m.get("tool_calls") for t in m["tool_calls"]}
    ids_answered = {m["tool_call_id"] for m in msgs if m["role"] == "tool"}
    assert ids_called == ids_answered  # no orphaned half of a pair


def test_summary_failure_keeps_the_full_history_and_the_turn_still_works(ctx, env):
    c = make_customer(ctx, 606)
    llm = Router(summary_error=True)
    agent = Agent(env, llm)
    chat(agent, ctx, c, 8)
    state = stored(agent, ctx, c)
    assert not state.get("summary") and len([m for m in state["messages"] if m["role"] == "user"]) == 8
    assert state["messages"][-1]["role"] == "assistant"  # replies still went out


def test_booking_facts_survive_compaction_because_they_live_in_state_not_in_messages(ctx, env):
    c = make_customer(ctx, 607)
    slot = offers(ctx, c, day=TUESDAY)[0]["slot_id"]
    router = Router()

    def llm(messages, **kw):
        if (
            kw.get("name") == "agent"
            and messages[-1]["role"] == "user"
            and "message 0" in messages[-1]["content"]
        ):
            return call(
                "book_appointment",
                {"slot_id": slot, "service_code": "ac_diagnostic", "idempotency_key": "bk-607-aaaa"},
            )
        if kw.get("name") == "agent" and messages[-1]["role"] == "tool":
            return say("You're booked!")
        return router(messages, **kw)

    agent = Agent(env, llm)
    chat(agent, ctx, c, 9)
    state = stored(agent, ctx, c)
    assert state["booked_ids"] and "compaction" in router.names()
    assert (
        json.dumps(state["messages"]).count("book_appointment") == 0
    )  # the tool exchange itself was compacted away
