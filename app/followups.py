"""Follow-ups: scheduled nudges for leads who have not booked, and a reminder before a booked visit.

Analogy: a tickler file. Each card says "on this date, check this customer". The `followups` table is
the card box (it survives restarts, one card per customer+rule). `tick()` is the clerk who goes through
the due cards once a minute and, for each one, RE-CHECKS the customer's situation before sending:
people book, say STOP, or get handed to a human between scheduling and sending.
"""

import logging
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app import gate
from app.availability import BUSINESS_TZ, local_label
from app.tools.common import clean_text
from app.tools.types import Caller, ToolContext

logger = logging.getLogger(__name__)
CONFIG = Path(__file__).resolve().parent.parent / "config" / "followups.yaml"
PAUSED = {"HANDOFF", "EMERGENCY"}


@lru_cache
def config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG.read_text())


def in_quiet_hours(now: datetime) -> bool:
    q = config()["quiet_hours"]
    return not (q["start"] <= now.astimezone(BUSINESS_TZ).hour < q["end"])


def next_window_start(now: datetime) -> datetime:
    """The next moment texting is allowed (today at start if it is early, else tomorrow)."""
    local = now.astimezone(BUSINESS_TZ)
    start = local.replace(hour=config()["quiet_hours"]["start"], minute=0, second=0, microsecond=0)
    return start if local < start else start + timedelta(days=1)


def schedule_lead_followups(ctx: ToolContext, customer_id: int, enquiry_at: datetime) -> None:
    """One card per rule. Calling it again for the same customer changes nothing (UNIQUE customer+rule)."""
    for rule, spec in config()["lead_rules"].items():
        ctx.conn.execute(
            "INSERT INTO followups (customer_id, rule, due_at) VALUES (%s, %s, %s)"
            " ON CONFLICT (customer_id, rule) DO NOTHING",
            (customer_id, rule, enquiry_at + timedelta(hours=spec["after_hours"])),
        )


def schedule_reminder(ctx: ToolContext, customer_id: int, booking_id: int, start_at: datetime) -> None:
    """One reminder card per booking. Rescheduling moves the same card (and re-arms it)."""
    due = max(ctx.now(), start_at - timedelta(hours=config()["reminder"]["before_hours"]))
    ctx.conn.execute(
        "INSERT INTO followups (customer_id, rule, due_at) VALUES (%s, %s, %s)"
        " ON CONFLICT (customer_id, rule) DO UPDATE SET due_at = EXCLUDED.due_at, status = 'PENDING',"
        " skip_reason = NULL, sent_at = NULL",
        (customer_id, f"reminder:{booking_id}", due),
    )


def cancel_reminder(ctx: ToolContext, customer_id: int, booking_id: int) -> None:
    ctx.conn.execute(
        "UPDATE followups SET status='CANCELLED' WHERE customer_id=%s AND rule=%s AND status='PENDING'",
        (customer_id, f"reminder:{booking_id}"),
    )


def _finish(ctx: ToolContext, fid: int, status: str, reason: str = "") -> None:
    ctx.conn.execute(
        "UPDATE followups SET status=%s, skip_reason=%s, sent_at=CASE WHEN %s='SENT' THEN %s END WHERE id=%s",
        (status, reason or None, status, ctx.now(), fid),
    )


def tick(ctx: ToolContext) -> dict[str, int]:
    """Handle every due card once. Returns counts, e.g. {"sent": 2, "skipped": 1, "deferred": 0}."""
    counts = {"sent": 0, "skipped": 0, "deferred": 0}
    now = ctx.now()
    due = ctx.conn.execute(
        "SELECT f.id, f.customer_id, f.rule, c.name, c.opted_out_at IS NOT NULL, c.lead_status"
        " FROM followups f JOIN customers c ON c.id = f.customer_id"
        " WHERE f.status = 'PENDING' AND f.due_at <= %s ORDER BY f.due_at FOR UPDATE OF f SKIP LOCKED",
        (now,),
    ).fetchall()
    for fid, cid, rule, name, opted_out, lead_status in due:
        is_reminder = rule.startswith("reminder:")
        reason = None
        when = ""
        if opted_out:
            reason = "opted_out"
        elif lead_status in PAUSED:
            reason = f"paused:{lead_status.lower()}"
        elif is_reminder:
            b = ctx.conn.execute(
                "SELECT status, start_at FROM bookings WHERE id=%s AND customer_id=%s",
                (int(rule.split(":", 1)[1]), cid),
            ).fetchone()
            if b is None or b[0] != "BOOKED" or b[1] <= now:
                reason = "booking_not_active"
            else:
                when = local_label(b[1])
        elif ctx.conn.execute(
            "SELECT 1 FROM bookings WHERE customer_id=%s AND status='BOOKED'", (cid,)
        ).fetchone():
            reason = "already_booked"
        if reason:
            _finish(ctx, fid, "SKIPPED", reason)
            counts["skipped"] += 1
            continue
        if in_quiet_hours(now):  # not a skip: try again when the window opens
            ctx.conn.execute("UPDATE followups SET due_at=%s WHERE id=%s", (next_window_start(now), fid))
            counts["deferred"] += 1
            continue
        text = (config()["reminder"] if is_reminder else config()["lead_rules"][rule])["text"]
        body = text.format(name=clean_text(name)[:40] if name else "there", when=when)
        row = ctx.conn.execute(
            "SELECT channel FROM inbound_messages WHERE customer_id=%s ORDER BY id DESC LIMIT 1", (cid,)
        ).fetchone()
        ctx.messaging.send(
            customer_id=cid,
            channel=row[0] if row else "sms",
            body=body,
            idempotency_key=f"followup:{cid}:{rule}",
        )
        gate.audit(ctx, Caller("system", cid), "followup_send", {"rule": rule}, "ALLOW", None, rule)
        _finish(ctx, fid, "SENT")
        counts["sent"] += 1
    logger.info("followup tick", extra=counts)
    return counts
