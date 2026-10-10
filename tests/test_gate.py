"""The permission matrix: every (tool x role) pair is checked against config/permissions.yaml."""

import pytest

from app.gate import _normalize, load_permissions
from app.tools import REGISTRY, Caller, run_tool
from tests.helpers import audit_rows, make_customer

ROLES = ["customer", "owner", "system"]
PERMS = load_permissions()["tools"]


def _level(tool: str, role: str) -> str:
    entry = PERMS.get(tool, {}).get(role)
    return "forbidden" if entry is None else _normalize(entry)[0]


PAIRS = [(t, r) for t in sorted(REGISTRY) for r in ROLES]


def test_yaml_and_registry_list_exactly_the_same_tools():
    assert set(PERMS) == set(REGISTRY)


@pytest.mark.parametrize(("tool", "role"), PAIRS)
def test_forbidden_pairs_are_refused_before_arguments_are_even_looked_at(ctx, tool, role):
    caller = make_customer(ctx, 1) if role == "customer" else Caller(role)
    res = run_tool(ctx, caller, tool, {"anything": "at all"})
    if _level(tool, role) == "forbidden":
        assert res["error"] == "forbidden", (tool, role, res)
        assert audit_rows(ctx, tool)[-1][1] == "DENY"
    else:
        assert res["error"] != "forbidden", (tool, role, res)  # allowed pairs get as far as validation


def test_the_system_role_has_no_tools_at_all():
    assert all(_level(t, "system") == "forbidden" for t in REGISTRY)


def test_customers_never_get_owner_tools(ctx):
    maria = make_customer(ctx, 1)
    for tool in ["list_leads", "list_jobs", "draft_bulk_followup", "send_bulk_followup"]:
        assert run_tool(ctx, maria, tool, {})["error"] == "forbidden", tool


def test_customers_have_no_tool_that_sends_to_an_arbitrary_number(ctx):
    names = " ".join(REGISTRY)
    assert (
        "send_sms" not in names and "send_message" not in names
    )  # least agency: replies go to the sender only
    res = run_tool(ctx, make_customer(ctx, 1), "send_sms", {"to": "+15125559999", "body": "hi"})
    assert res["error"] == "unknown_tool"
    assert audit_rows(ctx, "send_sms")[-1][1] == "DENY"


def test_unknown_tools_cannot_be_invented(ctx):
    maria = make_customer(ctx, 1)
    for name in ["delete_customer", "export_customers", "change_price", "run_sql"]:
        assert run_tool(ctx, maria, name, {"x": 1})["error"] == "unknown_tool"


def test_customer_without_a_verified_id_is_denied(ctx):
    res = run_tool(ctx, Caller("customer", None), "get_price", {"service_code": "ac_diagnostic"})
    assert res["error"] == "no_customer"


def test_money_tools_never_run_automatically_for_customers():
    for tool in ["request_discount", "request_refund"]:
        assert _level(tool, "customer") == "approval"


def test_audit_rows_never_contain_raw_phone_numbers(ctx):
    maria = make_customer(ctx, 1, ready=False)
    run_tool(ctx, maria, "update_my_contact", {"address": "call me on +1 512 555 0123 at the gate"})
    row = ctx.conn.execute(
        "SELECT args::text FROM audit_log WHERE tool = 'update_my_contact' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "5550123" not in row[0] and "ph_" in row[0]


def test_audit_log_cannot_be_edited_even_by_a_hijacked_tool_layer(ctx):
    import psycopg

    run_tool(ctx, make_customer(ctx, 1), "list_services", {})
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ctx.conn.execute("UPDATE audit_log SET decision = 'ALLOW'")
