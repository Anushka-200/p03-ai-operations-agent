"""Shared test helpers for the tool tests (real Postgres, fake calendar/CRM/messaging)."""

from datetime import UTC, date, datetime

from app.tools import Caller, ToolContext, run_tool

# Monday 2026-10-12, 09:00 in Chicago (14:00 UTC). Tuesday the 13th is the first bookable day.
NOW = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)
TUESDAY = date(2026, 10, 13)
SATURDAY = date(2026, 10, 17)
SUNDAY = date(2026, 10, 18)


def make_customer(ctx: ToolContext, n: int, ready: bool = True, **fields) -> Caller:
    """Insert a customer. ready=True gives them name/address/ZIP (a finished qualification)."""
    data = {"name": "Maria Lopez", "address": "1200 Oak St", "zip": "78704"} if ready else {}
    data.update(fields)
    cols = ["phone", "phone_hash", *data]
    vals = [f"+1512555{n:04d}", f"ph_t{n}", *data.values()]
    row = ctx.conn.execute(
        f"INSERT INTO customers ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
        vals,
    ).fetchone()
    return Caller("customer", row[0])


OWNER = Caller("owner")


def offers(ctx, caller, day=TUESDAY, service="ac_diagnostic", **kw):
    res = run_tool(ctx, caller, "find_slots", {"service_code": service, "date_from": str(day), **kw})
    assert res["ok"], res
    return res["slots"]


def book(ctx, caller, slot_id, key="key-abcdefgh", service="ac_diagnostic"):
    return run_tool(
        ctx, caller, "book_appointment", {"slot_id": slot_id, "service_code": service, "idempotency_key": key}
    )


def audit_rows(ctx, tool=None):
    sql = "SELECT tool, decision, reason, caller_role FROM audit_log"
    if tool:
        return ctx.conn.execute(sql + " WHERE tool = %s ORDER BY id", (tool,)).fetchall()
    return ctx.conn.execute(sql + " ORDER BY id").fetchall()
