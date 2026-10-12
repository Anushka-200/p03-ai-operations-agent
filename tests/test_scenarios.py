from collections import Counter

from app.tools import REGISTRY
from evals.scenario_schema import load_scenarios

SCENARIOS = load_scenarios()


def test_there_are_the_planned_scenarios_and_all_were_reviewed_by_the_owner():
    assert (
        len(SCENARIOS) == 22
    )  # the brief's 20 plus long-thread (compaction) and stored-injection (memory poisoning)
    # Only the owner flips `reviewed`. The owner read all 22 on 2026-10-12; a NEW scenario starts as False.
    assert all(s.reviewed is True for s in SCENARIOS)


def test_every_brief_category_is_covered():
    cats = Counter(s.category for s in SCENARIOS)
    for needed in [
        "normal",
        "incomplete_info",
        "double_booking",
        "duplicate",
        "emergency",
        "outage",
        "injection",
        "out_of_area",
        "faq",
        "opt_out",
        "long_thread",
        "privacy",
    ]:
        assert cats[needed] >= 1, needed
    assert cats["emergency"] == 3 and cats["injection"] == 2


def test_scenarios_only_name_real_tools():
    for s in SCENARIOS:
        for tool in s.expect.tools_in_order + s.expect.forbidden_tools:
            assert tool in REGISTRY, (s.id, tool)


def test_rule_handled_scenarios_make_no_llm_calls():
    for s in SCENARIOS:
        if s.category in {"emergency", "opt_out"}:
            assert s.expect.llm_calls == 0 and s.expect.max_tool_calls == 0, s.id


def test_every_scenario_can_be_checked_by_something():
    for s in SCENARIOS:
        e = s.expect
        assert e.final_state or e.reply_must_match or e.reply_must_not_match or e.tools_in_order, s.id


def test_regexes_compile():
    import re

    for s in SCENARIOS:
        for pattern in s.expect.reply_must_match + s.expect.reply_must_not_match:
            re.compile(pattern)
