"""Supervisor mode (ADR 006): routing, least agency per specialist, and 'single' mode unchanged."""

from app.agent import specialists
from app.agent.graph import customer_tools
from app.tools import REGISTRY, Caller
from tests.helpers import make_customer
from tests.test_agent import (  # noqa: F401, F811  (agent_env is a pytest fixture)
    FakeLLM,
    agent_env,
    say,
    turn,
)

CUSTOMER_TOOLS = {t["function"]["name"] for t in customer_tools(Caller("customer", 1))}


def _tool_names(llm_call):
    return {t["function"]["name"] for t in llm_call[1]["tools"]}


def test_every_specialist_tool_exists_and_together_they_cover_what_a_customer_can_use():
    for tools in specialists.TOOLS.values():
        assert tools <= set(REGISTRY)
    assert set().union(*specialists.TOOLS.values()) == CUSTOMER_TOOLS  # nothing lost, nothing invented


def test_money_tools_exist_only_for_billing_and_booking_tools_only_for_intake():
    money = {"request_discount", "request_refund", "cancel_appointment"}
    booking = {"book_appointment", "find_slots", "reschedule_appointment"}
    assert (
        money <= specialists.TOOLS[specialists.BILLING] and not money & specialists.TOOLS[specialists.INTAKE]
    )
    assert (
        booking <= specialists.TOOLS[specialists.INTAKE]
        and not booking & specialists.TOOLS[specialists.BILLING]
    )


def test_supervisor_routes_a_discount_question_to_billing_which_sees_only_billing_tools(ctx, agent_env):  # noqa: F811
    make, _, _ = agent_env
    ctx.settings = ctx.settings.model_copy(update={"agent_mode": "supervisor", "agent_tool_mode": "classic"})
    c = make_customer(ctx, 821)
    llm = FakeLLM(say("BILLING"), say("I'll ask the owner."))
    reply = turn(make(llm), ctx, c, "can I get 10% off?")
    assert "ask the owner" in reply
    router_call, agent_call = llm.calls
    assert router_call[1]["name"] == "supervisor-route" and "tools" not in router_call[1]
    assert _tool_names(agent_call) == set(specialists.TOOLS[specialists.BILLING])
    assert "BILLING and SUPPORT specialist" in agent_call[0][0]["content"]


def test_supervisor_routes_booking_talk_to_intake_and_a_router_failure_falls_back_to_intake(ctx, agent_env):  # noqa: F811
    make, _, _ = agent_env
    ctx.settings = ctx.settings.model_copy(update={"agent_mode": "supervisor", "agent_tool_mode": "classic"})
    c = make_customer(ctx, 822)
    llm = FakeLLM(say("INTAKE"), say("Which day works?"))
    turn(make(llm), ctx, c, "I need a tune-up next week")
    assert _tool_names(llm.calls[1]) == set(specialists.TOOLS[specialists.INTAKE])

    from app.llm import LLMError

    llm2 = FakeLLM(LLMError("down"), say("Which day works?"))
    turn(make(llm2), ctx, c, "hello again")
    assert _tool_names(llm2.calls[1]) == set(specialists.TOOLS[specialists.INTAKE])


def test_an_owner_decision_note_goes_to_billing_without_asking_the_router(ctx):
    llm = FakeLLM()  # any call would raise IndexError
    name, res = specialists.route(
        llm,
        [{"role": "user", "content": "[system] The owner decided on your pending request(s): {}"}],
        ctx.settings,
    )
    assert name == specialists.BILLING and res is None and llm.calls == []


def test_single_mode_makes_no_router_call_and_keeps_every_tool(ctx, agent_env):  # noqa: F811
    make, _, _ = agent_env
    ctx.settings = ctx.settings.model_copy(update={"agent_tool_mode": "classic"})
    c = make_customer(ctx, 823)
    llm = FakeLLM(say("Hello!"))
    turn(make(llm), ctx, c, "hi")
    assert (
        len(llm.calls) == 1 and _tool_names(llm.calls[0]) == CUSTOMER_TOOLS
    )  # classic tools: nothing hidden


# ---------- E3 lean tool mode and E1 agent model (settings only; defaults keep today's behaviour) ----------


def test_update_my_contact_with_a_zip_also_answers_whether_we_serve_it(ctx):
    from app.tools import run_tool

    c = make_customer(ctx, 824, ready=False)
    inside = run_tool(ctx, c, "update_my_contact", {"zip": "78704"})
    assert inside["ok"] and inside["service_area"]["in_area"] is True
    outside = run_tool(ctx, c, "update_my_contact", {"zip": "10001"})
    assert outside["service_area"]["in_area"] is False
    assert "service_area" not in run_tool(ctx, c, "update_my_contact", {"name": "Maria"})


def test_lean_mode_hides_the_three_lookup_tools_and_puts_the_price_table_in_the_prompt(ctx, agent_env):  # noqa: F811
    make, _, _ = agent_env
    ctx.settings = ctx.settings.model_copy(update={"agent_tool_mode": "lean"})
    c = make_customer(ctx, 825)
    llm = FakeLLM(say("Hello!"))
    turn(make(llm), ctx, c, "hi")
    names = _tool_names(llm.calls[0])
    assert not names & {"list_services", "get_price", "check_service_area"} and "find_slots" in names
    system = llm.calls[0][0][0]["content"]
    assert (
        "Our services (code | name | price" in system
        and "$89" in system
        and "check_service_area" not in system
    )


def test_classic_mode_keeps_all_lookup_tools_and_no_catalog(ctx, agent_env):  # noqa: F811
    make, _, _ = agent_env
    ctx.settings = ctx.settings.model_copy(update={"agent_tool_mode": "classic"})
    c = make_customer(ctx, 826)
    llm = FakeLLM(say("Hello!"))
    turn(make(llm), ctx, c, "hi")
    assert {"list_services", "get_price", "check_service_area"} <= _tool_names(llm.calls[0])
    assert "Our services (code" not in llm.calls[0][0][0]["content"]


def test_a_price_from_the_prompt_catalog_passes_the_output_guard_in_lean_mode(ctx, agent_env):  # noqa: F811
    make, _, _ = agent_env
    ctx.settings = ctx.settings.model_copy(update={"agent_tool_mode": "lean"})
    c = make_customer(ctx, 827)
    llm = FakeLLM(say("The AC diagnostic visit is $89."))
    assert "$89" in turn(make(llm), ctx, c, "how much is a diagnostic?")
    assert len(llm.calls) == 1  # no repair call: the price table is allowed even without a tool result


def test_agent_model_override_applies_to_the_agent_step_only(ctx, agent_env):  # noqa: F811
    make, _, _ = agent_env
    ctx.settings = ctx.settings.model_copy(
        update={"llm_agent_model": "claude-sonnet-5-5", "agent_mode": "supervisor"}
    )
    c = make_customer(ctx, 828)
    llm = FakeLLM(say("INTAKE"), say("Hello!"))
    turn(make(llm), ctx, c, "hi")
    assert llm.calls[0][1].get("model") is None  # the router stays on the default cheap model
    assert llm.calls[1][1]["model"] == "claude-sonnet-5-5"
