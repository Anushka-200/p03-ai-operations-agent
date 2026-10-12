"""Customers a human must look at: emergencies and handoffs, and the owner's way to hand them back.

Analogy: a "needs a person" tray on the front desk. The agent can put a folder in the tray
(escalate / handoff_to_human) but only a human can take it out. That is why `clear_case` is a plain
function called by the owner's Telegram bot and is NOT a registered tool: the model can never call it.
"""

from typing import Any

from app import gate
from app.tools.common import clean_text
from app.tools.types import Caller, ToolContext

OPEN_STATUSES = ("HANDOFF", "EMERGENCY")


def list_cases(ctx: ToolContext) -> list[dict[str, Any]]:
    rows = ctx.conn.execute(
        "SELECT c.id, c.name, c.phone, c.lead_status,"
        " COALESCE((SELECT max(e.created_at) FROM emergencies e WHERE e.customer_id = c.id AND e.cleared_at IS NULL),"
        "          (SELECT max(a.ts) FROM audit_log a WHERE a.customer_id = c.id AND a.tool = 'handoff_to_human')) AS since"
        " FROM customers c WHERE c.lead_status = ANY(%s) ORDER BY since NULLS LAST, c.id LIMIT 20",
        (list(OPEN_STATUSES),),
    ).fetchall()
    return [
        {
            "customer_id": r[0],
            "name": clean_text(r[1])[:60] if r[1] else None,
            "phone": r[2],
            "status": r[3],
            "since": r[4],
        }
        for r in rows
    ]


def describe_cases(cases: list[dict[str, Any]]) -> str:
    if not cases:
        return "No open cases. Nobody is waiting for a person."
    lines = ["Open cases (the agent is paused for these customers):"]
    for c in cases:
        who = c["name"] or "(name not given)"
        since = f" since {c['since']:%a %H:%M} UTC" if c["since"] else ""
        lines.append(f"#{c['customer_id']} {who}, {c['phone']}: {c['status']}{since}")
    lines.append("When you have dealt with one, send /resume <number> to let the agent talk to them again.")
    return "\n".join(lines)


def clear_case(ctx: ToolContext, customer_id: int, cleared_by: str) -> dict[str, Any]:
    """Hand a customer back to the agent. Audited. Returns {"ok": True, "status": new status} or an error."""
    with ctx.conn.transaction():
        row = ctx.conn.execute(
            "SELECT lead_status FROM customers WHERE id = %s FOR UPDATE", (customer_id,)
        ).fetchone()
        if row is None:
            return {"ok": False, "message": f"No customer #{customer_id}."}
        if row[0] not in OPEN_STATUSES:
            return {
                "ok": False,
                "message": f"Customer #{customer_id} is not waiting for a person (status {row[0]}).",
            }
        booked = ctx.conn.execute(
            "SELECT 1 FROM bookings WHERE customer_id = %s AND status = 'BOOKED' AND end_at > %s LIMIT 1",
            (customer_id, ctx.now()),
        ).fetchone()
        new_status = "BOOKED" if booked else "QUALIFYING"
        ctx.conn.execute("UPDATE customers SET lead_status = %s WHERE id = %s", (new_status, customer_id))
        ctx.conn.execute(
            "UPDATE emergencies SET cleared_at = %s, cleared_by = %s WHERE customer_id = %s AND cleared_at IS NULL",
            (ctx.now(), cleared_by, customer_id),
        )
    gate.audit(
        ctx, Caller("owner"), "clear_case", {"customer_id": customer_id, "was": row[0], "now": new_status},
        "ALLOW", {"ok": True}, f"cleared by {cleared_by}",
    )  # fmt: skip
    return {"ok": True, "status": new_status, "was": row[0]}
