"""The eval runner itself must be trustworthy: a grader that cannot fail proves nothing.

These tests run the rule-handled scenarios for real ($0, no model), and show that wrong expectations,
forbidden tools and wrong ordering are all caught. Scenarios that need the model are exercised by the
actual eval run, which costs money and needs the owner's OK."""

import psycopg
import pytest

from app.cost_guard import SpendTracker
from app.settings import get_settings
from app.tools import run_tool
from evals.run import EVAL_NOW, RunResult, check_final_state, grade, percentile, projection, run_scenario
from evals.scenario_schema import load_scenarios
from tests.helpers import make_customer, offers

SCENARIOS = {s.id: s for s in load_scenarios()}


class NoAgent:
    """Rule-handled scenarios never reach the agent; if one does, this fails loudly."""

    llm = None

    def run_turn(self, *a, **k):
        raise AssertionError("the agent must not run for a rule-handled scenario")


@pytest.fixture
def db():
    try:
        conn = psycopg.connect(get_settings().database_url, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("Postgres is not running")
    yield conn
    conn.rollback()
    conn.close()


@pytest.mark.parametrize("sid", ["emergency_gas", "emergency_flood_with_discount_ask", "emergency_no_heat_freezing", "stop_optout"])
def test_rule_handled_scenarios_pass_end_to_end_with_zero_model_calls(db, sid):
    r = run_scenario(SCENARIOS[sid], 0, 0, db, NoAgent(), None, SpendTracker(1.0))  # type: ignore[arg-type]
    assert r.passed, r.failures
    assert r.llm_calls == 0 and r.cost_usd == 0.0


def test_the_runner_leaves_nothing_behind(db):
    run_scenario(SCENARIOS["emergency_gas"], 0, 0, db, NoAgent(), None, SpendTracker(1.0))  # type: ignore[arg-type]
    assert db.execute("SELECT count(*) FROM customers").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM emergencies").fetchone()[0] == 0


def test_a_wrong_expectation_makes_the_scenario_fail(db):
    sc = SCENARIOS["emergency_gas"].model_copy(deep=True)
    sc.expect.final_state["emergency_rows"] = 2
    sc.expect.reply_must_match = ["this text is never sent"]
    r = run_scenario(sc, 0, 0, db, NoAgent(), None, SpendTracker(1.0))  # type: ignore[arg-type]
    assert not r.passed
    assert any("emergency_rows" in f for f in r.failures) and any("no reply matches" in f for f in r.failures)


def test_final_state_operators_and_unknown_keys():
    facts = {"bookings_for_customer": 1, "booking_service": "ac_tuneup", "booking_hour": 9, "customer_address": "77 Walnut Ave"}
    assert check_final_state({"bookings_for_customer": 1, "bookings_for_customer_lte": 1, "booking_hour_lt": 12, "booking_hour_ge": 9}, facts) == []
    assert check_final_state({"booking_service_in": ["ac_tuneup", "furnace_tuneup"]}, facts) == []
    assert check_final_state({"bookings_for_customer_lte": 0}, facts) != []
    unknown = check_final_state({"mystery_key": True}, facts)
    assert unknown and "no such fact" in unknown[0]  # an unknown check FAILS: it must never silently pass


def test_grade_catches_a_forbidden_tool_that_succeeded_and_a_wrong_order(ctx):
    c = make_customer(ctx, 960)
    slot = offers(ctx, c)[0]["slot_id"]
    run_tool(ctx, c, "book_appointment", {"slot_id": slot, "service_code": "ac_diagnostic", "idempotency_key": "bk-960-aaaa"})
    run_tool(ctx, c, "find_slots", {"service_code": "ac_diagnostic", "date_from": "2026-10-14"})
    sc = SCENARIOS["normal_booking_ac"].model_copy(deep=True)
    sc.expect.forbidden_tools = ["book_appointment"]
    sc.expect.tools_in_order = ["find_slots", "book_appointment"]  # they happened in the opposite order after find
    sc.expect.final_state = {}
    sc.expect.reply_must_match = sc.expect.reply_must_not_match = []
    meta = {"cid": c.customer_id, "other": None}
    failures, stats = grade(ctx, sc, meta, "sid", [], llm_calls=0)
    assert stats["forbidden_executed"] == 1
    assert any("forbidden tool executed" in f for f in failures)
    sc.expect.forbidden_tools = []
    sc.expect.tools_in_order = ["find_slots", "find_slots", "find_slots"]
    failures, _ = grade(ctx, sc, meta, "sid", [], llm_calls=0)
    assert any("out of order" in f for f in failures)


def test_cost_projection_is_zero_for_rules_and_asks_for_approval_for_the_full_run():
    rules = [s for s in SCENARIOS.values() if s.expect.llm_calls == 0]
    assert projection(rules, 3)[0] == 0.0
    full, llm_runs = projection(list(SCENARIOS.values()), 3)
    assert llm_runs > 0 and full > get_settings().cost_confirm_above_usd


def test_percentile_and_result_defaults():
    assert percentile([], 0.5) == 0.0 and percentile([1, 2, 3, 4, 5], 0.5) == 3 and percentile([1, 2, 3, 4, 5], 0.95) == 5
    assert RunResult("x", 0, False).failures == [] and EVAL_NOW.tzinfo is not None
