"""The agent graph (ADR 001). Small on purpose: the conversation is the LLM's job, every rule is code.

    agent --tool calls--> tools --> agent          (loop; capped)
      |  final text                |
      v                            +--cap hit--> handoff --> END
    guard --pending approval--> approval_wait (pauses here) --> agent
      |  nothing pending
      v
     END

State lives in Postgres checkpoints, so a restart between two customer messages (or while waiting
for the owner) loses nothing. Everything with side effects goes through `run_tool`, i.e. the gate.
"""

import json
import logging
import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app import gate
from app.agent import guard, prompts
from app.llm import LLMError
from app.tools import REGISTRY, Caller, ToolContext, run_tool

logger = logging.getLogger(__name__)

HANDOFF_TEXT = (
    "Thanks for your patience. I'm passing this to a team member who will text you back shortly."
)
LLM_DOWN_TEXT = "Sorry, I'm having trouble right now. A team member will text you back shortly."


class State(TypedDict, total=False):
    messages: Annotated[list[dict[str, Any]], operator.add]  # the conversation (OpenAI-shaped dicts)
    # per message (reset by the input of each turn)
    turn_tool_calls: int
    reply: str | None
    draft: str | None
    pending_approvals: list[int]
    handoff_reason: str | None
    # per conversation (carried across turns by the checkpoint)
    conv_tool_calls: int
    cost_usd: float
    tokens: int
    disclosed: bool
    booked_ids: list[int]
    amounts_seen: list[float]
    approvals_resolved: list[int]


def _cfg(config) -> dict[str, Any]:
    return config["configurable"]


def customer_tools(caller: Caller) -> list[dict[str, Any]]:
    """Tool schemas the model may see: only what this role can use at all (least agency)."""
    out = []
    for name, spec in sorted(REGISTRY.items()):
        if gate.role_decision(name, caller.role) is None:
            schema = spec.args_model.model_json_schema()
            schema.pop("title", None)
            out.append({"type": "function", "function": {"name": name, "description": spec.description, "parameters": schema}})
    return out


def _profile(ctx: ToolContext, caller: Caller) -> dict[str, str | None]:
    row = ctx.conn.execute("SELECT name, address, zip FROM customers WHERE id=%s", (caller.customer_id,)).fetchone()
    return {"name": row[0], "address": row[1], "zip": row[2]} if row else {}


def _price_table(ctx: ToolContext) -> set[float]:
    return {float(r[0]) for r in ctx.conn.execute("SELECT price_usd FROM services WHERE active").fetchall()}


def _spent(state: State, res) -> dict[str, Any]:
    return {
        "cost_usd": state.get("cost_usd", 0.0) + (res.cost_usd or 0.0),
        "tokens": state.get("tokens", 0) + res.input_tokens + res.output_tokens,
    }


def _over_caps(state: State, ctx: ToolContext, adding: int = 0) -> str | None:
    s = ctx.settings
    if state.get("turn_tool_calls", 0) + adding > s.agent_max_tool_calls_per_message:
        return f"more than {s.agent_max_tool_calls_per_message} tool calls for one message"
    if state.get("conv_tool_calls", 0) + adding > s.agent_max_tool_calls_per_conversation:
        return f"more than {s.agent_max_tool_calls_per_conversation} tool calls in this conversation"
    if state.get("cost_usd", 0.0) >= s.agent_max_cost_per_conversation_usd:
        return f"conversation cost reached ${s.agent_max_cost_per_conversation_usd:.2f}"
    return None


def agent_node(state: State, config) -> dict[str, Any]:
    c = _cfg(config)
    ctx, caller, llm = c["ctx"], c["caller"], c["llm"]
    if (why := _over_caps(state, ctx)) is not None:
        return {"handoff_reason": why}
    system = prompts.system_prompt(ctx.now(), _profile(ctx, caller))
    try:
        res = llm(
            [{"role": "system", "content": system}, *state["messages"]],
            name="agent", max_tokens=ctx.settings.agent_max_output_tokens,
            tools=customer_tools(caller), settings=ctx.settings,
        )  # fmt: skip
    except LLMError:
        logger.exception("agent llm failed")
        return {"handoff_reason": "the language model was unavailable", "draft": None}
    out: dict[str, Any] = _spent(state, res)
    if res.tool_calls:
        if (why := _over_caps(state, ctx, adding=len(res.tool_calls))) is not None:
            return {**out, "handoff_reason": why}
        out["messages"] = [res.message]
    else:
        out["draft"] = res.text  # the guard appends the final text after checking it
    return out


def after_agent(state: State) -> str:
    if state.get("handoff_reason"):
        return "handoff"
    return "guard" if state.get("draft") is not None else "tools"


def tools_node(state: State, config) -> dict[str, Any]:
    c = _cfg(config)
    ctx, caller = c["ctx"], c["caller"]
    calls = state["messages"][-1]["tool_calls"]
    results, pending = [], list(state.get("pending_approvals", []))
    booked, amounts = list(state.get("booked_ids", [])), list(state.get("amounts_seen", []))
    for call in calls:
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
            result = run_tool(ctx, caller, name, args)
        except ValueError:
            result = {"ok": False, "error": "invalid_arguments", "message": "Arguments must be a JSON object."}
        if result.get("error") == "needs_approval":
            pending.append(result["approval_id"])
        if result.get("ok") and result.get("booking_id") and result.get("status") == "BOOKED":
            booked.append(result["booking_id"])
        _collect_amounts(result, amounts)
        results.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, default=str)})
    return {
        "messages": results,
        "turn_tool_calls": state.get("turn_tool_calls", 0) + len(calls),
        "conv_tool_calls": state.get("conv_tool_calls", 0) + len(calls),
        "pending_approvals": pending, "booked_ids": booked, "amounts_seen": amounts,
    }  # fmt: skip


def _collect_amounts(obj: Any, into: list[float]) -> None:
    """Prices the model is allowed to repeat: any money value a tool itself returned."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.endswith("_usd") and isinstance(v, (int, float)):
                into.append(float(v))
            else:
                _collect_amounts(v, into)
    elif isinstance(obj, list):
        for v in obj:
            _collect_amounts(v, into)


def guard_node(state: State, config) -> dict[str, Any]:
    c = _cfg(config)
    ctx, llm = c["ctx"], c["llm"]
    text = (state.get("draft") or "").strip()
    allowed = _price_table(ctx) | set(state.get("amounts_seen", []))
    booked = state.get("booked_ids", [])
    out: dict[str, Any] = {}
    problem = guard.check_reply(text, allowed, booked) if text else "the reply was empty"
    if problem:
        logger.warning("output guard blocked a reply", extra={"problem": problem, "draft_preview": text[:160]})
        try:  # one repair attempt, then a safe fallback: never loop on a misbehaving model
            res = llm(
                [{"role": "system", "content": prompts.system_prompt(ctx.now(), _profile(ctx, c["caller"]))},
                 *state["messages"],
                 {"role": "user", "content": f"[system] Your draft was blocked because {problem} Rewrite the reply to the customer now."}],
                name="agent-repair", max_tokens=ctx.settings.agent_max_output_tokens, settings=ctx.settings,
            )  # fmt: skip
            out.update(_spent(state, res))
            text = res.text.strip()
            problem = guard.check_reply(text, allowed, booked) if text else "empty"
        except LLMError:
            problem = "llm unavailable"
        if problem:
            text = guard.SAFE_FALLBACK
            out["handoff_reason"] = "reply failed the output guard twice"
    if not state.get("disclosed"):  # AI disclosure is code, not a prompt wish
        text = "Hi, I'm CoolBreeze's virtual assistant. " + text
        out["disclosed"] = True
    out.update({"messages": [{"role": "assistant", "content": text}], "reply": text, "draft": None})
    return out


def after_guard(state: State) -> str:
    if state.get("handoff_reason"):
        return "handoff"
    return "approval_wait" if state.get("pending_approvals") else "end"


def approval_note(decision: dict[str, Any]) -> str:
    return (
        f"[system] The owner decided on your pending request(s): {json.dumps(decision, default=str)}. "
        "Tell the customer the outcome in 1-2 sentences."
    )


def approval_wait_node(state: State, config) -> dict[str, Any]:
    """Pause until the owner decides. Nothing with side effects happens before interrupt(), because
    LangGraph re-runs this node from the top when it resumes."""
    decision = interrupt({"approval_ids": state.get("pending_approvals", [])})
    note = approval_note(decision)
    amounts = list(state.get("amounts_seen", []))
    _collect_amounts(decision, amounts)  # the owner's decision can carry prices the agent may repeat
    return {
        "messages": [{"role": "user", "content": note}],
        "amounts_seen": amounts,
        "pending_approvals": [],
        "approvals_resolved": [*state.get("approvals_resolved", []), *state.get("pending_approvals", [])],
        "reply": None,
    }


def handoff_node(state: State, config) -> dict[str, Any]:
    c = _cfg(config)
    ctx, caller = c["ctx"], c["caller"]
    reason = state.get("handoff_reason") or "limit reached"
    run_tool(ctx, caller, "handoff_to_human", {"reason": f"agent stopped: {reason}"})
    gate.audit(ctx, caller, "agent_cap_handoff", {"reason": reason}, "ALLOW", None, reason)
    text = state.get("reply") or HANDOFF_TEXT
    msgs = [] if state.get("reply") else [{"role": "assistant", "content": text}]
    return {"messages": msgs, "reply": text, "handoff_reason": None, "pending_approvals": []}


def build_graph(checkpointer):
    g = StateGraph(State)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.add_node("guard", guard_node)
    g.add_node("approval_wait", approval_wait_node)
    g.add_node("handoff", handoff_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", after_agent, {"tools": "tools", "guard": "guard", "handoff": "handoff"})
    g.add_conditional_edges("tools", lambda s: "agent", {"agent": "agent"})
    g.add_conditional_edges("guard", after_guard, {"approval_wait": "approval_wait", "handoff": "handoff", "end": END})
    g.add_edge("approval_wait", "agent")
    g.add_edge("handoff", END)
    return g.compile(checkpointer=checkpointer)
