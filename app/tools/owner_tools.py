"""Tools for the owner assistant (Telegram / Claude Desktop). Mostly read-only; bulk sending needs approval."""

from datetime import date, datetime, time, timedelta
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import Field

from app.availability import BUSINESS_TZ, local_label
from app.tools.common import ToolArgs, clean_text
from app.tools.registry import register
from app.tools.types import Caller, ToolContext, ToolError


class LeadsArgs(ToolArgs):
    status: str | None = Field(
        default=None, description="NEW, QUALIFYING, BOOKED, HANDOFF... (omit for all unbooked leads)"
    )
    since_days: int = Field(default=1, ge=0, le=30, description="Created within the last N days")


@register(
    "list_leads",
    LeadsArgs,
    "List leads (customers) created in the last N days, optionally by status. By default it shows "
    "leads that have NOT booked. Phone numbers are never shown.",
)
def list_leads(ctx: ToolContext, caller: Caller, a: LeadsArgs) -> dict[str, Any]:
    since = ctx.now() - timedelta(days=a.since_days)
    sql = "SELECT id, name, lead_status, created_at, opted_out_at FROM customers WHERE created_at >= %s"
    params: list[Any] = [since]
    if a.status:
        sql += " AND lead_status = %s"
        params.append(a.status.upper())
    else:
        sql += " AND lead_status <> 'BOOKED'"
    rows = ctx.conn.execute(sql + " ORDER BY created_at DESC LIMIT 25", params).fetchall()
    return {
        "ok": True,
        "count": len(rows),
        "leads": [
            {
                "customer_id": r[0],
                "name": r[1] or "(name not given yet)",
                "status": r[2],
                "since": local_label(r[3]),
                "opted_out": r[4] is not None,
            }
            for r in rows
        ],
    }


class JobsArgs(ToolArgs):
    day: date = Field(description="Day to list, YYYY-MM-DD (local time)")


@register(
    "list_jobs", JobsArgs, "List the booked visits on one day with customer name, service and technician."
)
def list_jobs(ctx: ToolContext, caller: Caller, a: JobsArgs) -> dict[str, Any]:
    start = datetime.combine(a.day, time(0), tzinfo=BUSINESS_TZ)
    rows = ctx.conn.execute(
        "SELECT b.id, b.start_at, b.service_code, c.name, t.name, b.price_usd, b.discount_pct"
        " FROM bookings b JOIN customers c ON c.id = b.customer_id JOIN technicians t ON t.id = b.technician_id"
        " WHERE b.status = 'BOOKED' AND b.start_at >= %s AND b.start_at < %s ORDER BY b.start_at",
        (start, start + timedelta(days=1)),
    ).fetchall()
    return {
        "ok": True,
        "day": str(a.day),
        "count": len(rows),
        "jobs": [
            {
                "booking_id": r[0],
                "when": local_label(r[1]),
                "service": r[2],
                "customer": r[3] or "(no name)",
                "technician": r[4],
                "total_usd": round(float(r[5]) * (1 - float(r[6]) / 100), 2),
            }
            for r in rows
        ],
    }


class DraftArgs(ToolArgs):
    since_days: int = Field(default=7, ge=1, le=30)
    message: str = Field(min_length=10, max_length=320, description="The text to send to each lead")


@register(
    "draft_bulk_followup",
    DraftArgs,
    "Prepare (NOT send) a follow-up text to every lead from the last N days who has not booked and has "
    "not opted out. Returns a preview. Nothing is sent until send_bulk_followup is approved by the owner.",
)
def draft_bulk_followup(ctx: ToolContext, caller: Caller, a: DraftArgs) -> dict[str, Any]:
    ids = [
        r[0]
        for r in ctx.conn.execute(
            "SELECT id FROM customers WHERE created_at >= %s AND lead_status NOT IN ('BOOKED', 'HANDOFF', 'EMERGENCY')"
            " AND opted_out_at IS NULL ORDER BY id LIMIT 200",
            (ctx.now() - timedelta(days=a.since_days),),
        )
    ]
    draft_id = ctx.conn.execute(
        "INSERT INTO approvals (kind, payload, requested_by, status) VALUES ('bulk_followup_draft', %s, %s, 'DRAFT')"
        " RETURNING id",
        (Jsonb({"recipients": ids, "message": clean_text(a.message)}), caller.role),
    ).fetchone()[0]  # type: ignore[index]
    return {
        "ok": True,
        "draft_id": draft_id,
        "recipient_count": len(ids),
        "preview": clean_text(a.message),
        "next_step": "To send, call send_bulk_followup with this draft_id. The owner must approve it first.",
    }


class SendBulkArgs(ToolArgs):
    draft_id: int


def _precheck_draft(ctx: ToolContext, caller: Caller, a: SendBulkArgs) -> None:
    row = ctx.conn.execute(
        "SELECT status FROM approvals WHERE id = %s AND kind = 'bulk_followup_draft'", (a.draft_id,)
    ).fetchone()
    if row is None or row[0] != "DRAFT":
        raise ToolError("draft_not_found", f"No unsent draft #{a.draft_id}. Call draft_bulk_followup first.")


@register(
    "send_bulk_followup",
    SendBulkArgs,
    "Send a prepared follow-up draft to its recipients. ALWAYS needs the owner's approval first.",
    precheck=_precheck_draft,
)
def send_bulk_followup(ctx: ToolContext, caller: Caller, a: SendBulkArgs) -> dict[str, Any]:
    _precheck_draft(ctx, caller, a)
    payload = ctx.conn.execute(
        "SELECT payload FROM approvals WHERE id = %s FOR UPDATE", (a.draft_id,)
    ).fetchone()[0]  # type: ignore[index]
    sent = skipped = 0
    for cid in payload["recipients"]:
        row = ctx.conn.execute(
            "SELECT opted_out_at, lead_status FROM customers WHERE id = %s", (cid,)
        ).fetchone()
        # Re-check at send time: a lead may have booked or sent STOP since the draft was made.
        if row is None or row[0] is not None or row[1] == "BOOKED":
            skipped += 1
            continue
        ctx.messaging.send(
            customer_id=cid,
            channel="sms",
            body=payload["message"],
            idempotency_key=f"bulk:{a.draft_id}:{cid}",
        )
        sent += 1
    ctx.conn.execute("UPDATE approvals SET status = 'EXPIRED' WHERE id = %s", (a.draft_id,))  # draft consumed
    return {
        "ok": True,
        "queued": sent,
        "skipped": skipped,
        "dry_run": getattr(ctx.messaging, "dry_run", None),
    }
