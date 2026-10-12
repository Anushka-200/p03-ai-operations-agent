"""What happens to one inbound customer message (the part Twilio's HTTP request does not wait for).

Two phases, on purpose (P01's "save first" pattern):
  1. `save_inbound`  : runs inside the webhook request. Find/create the customer, store the message.
                       A repeated MessageSid is detected here by the database's UNIQUE constraint.
                       Fast, so we can answer Twilio in well under a second.
  2. `process_inbound`: runs afterwards in the background. Rules first (STOP, emergency), then the agent.
                       If this crashes, the message is already safe in the table with processed_at NULL,
                       so it can be retried instead of lost.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg

from app import emergency, followups
from app.concurrency import lock_conversation
from app.privacy import normalize_phone, phone_hash
from app.tools.types import Caller, ToolContext

logger = logging.getLogger(__name__)

# Carrier-standard opt-out words. Anything else (even "stop calling me at night") goes to the agent.
STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
STOP_REPLY = "You are unsubscribed from CoolBreeze HVAC messages. Reply START to opt back in."
START_WORDS = {"START", "UNSTOP", "YES"}
START_REPLY = "You are opted back in. How can we help?"

# An agent turn: (ctx, caller, inbound_id, text) -> reply text for the verified sender (or None).
AgentFn = Callable[[ToolContext, Caller, int, str], str | None]


@dataclass
class Saved:
    inbound_id: int | None  # None = duplicate delivery
    customer_id: int
    channel: str
    duplicate: bool


def parse_sender(raw_from: str) -> tuple[str, str]:
    """'whatsapp:+1512...' -> ('whatsapp', '+1512...'); plain numbers are SMS."""
    if raw_from.lower().startswith("whatsapp:"):
        return "whatsapp", normalize_phone(raw_from.split(":", 1)[1])
    return "sms", normalize_phone(raw_from)


def save_inbound(conn: psycopg.Connection, sid: str, raw_from: str, body: str) -> Saved:
    channel, phone = parse_sender(raw_from)
    cid = conn.execute(
        "INSERT INTO customers (phone, phone_hash) VALUES (%s, %s)"
        " ON CONFLICT (phone) DO UPDATE SET phone = EXCLUDED.phone RETURNING id",
        (phone, phone_hash(phone)),
    ).fetchone()
    assert cid is not None  # RETURNING always yields the row (inserted or updated)
    cid = cid[0]
    row = conn.execute(
        "INSERT INTO inbound_messages (message_sid, customer_id, channel, body) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (message_sid) DO NOTHING RETURNING id",
        (sid, cid, channel, body[:4000]),
    ).fetchone()
    conn.commit()  # committed BEFORE we tell Twilio "got it"
    return Saved(row[0] if row else None, cid, channel, duplicate=row is None)


def process_inbound(ctx: ToolContext, saved: Saved, body: str, agent_fn: AgentFn | None = None) -> dict[str, Any]:
    """Run the rules, then (if nothing caught the message) the agent. Returns a small outcome dict."""
    assert saved.inbound_id is not None, "duplicates are filtered out before processing"
    cid = saved.customer_id
    send = ctx.messaging.send
    key = f"in:{saved.inbound_id}"
    lock_conversation(ctx.conn, cid)  # a second message from this customer waits here until we commit
    cust = ctx.conn.execute("SELECT lead_status, opted_out_at IS NOT NULL FROM customers WHERE id=%s", (cid,)).fetchone()
    assert cust is not None
    status, opted_out = cust
    word = re.sub(r"[^A-Za-z]", "", body).upper()

    outcome: dict[str, Any]
    if word in STOP_WORDS:
        # Permanent until the customer says START. Cancel pending follow-ups so none fire later.
        ctx.conn.execute("UPDATE customers SET opted_out_at = COALESCE(opted_out_at, %s) WHERE id=%s", (ctx.now(), cid))
        ctx.conn.execute("UPDATE followups SET status='CANCELLED' WHERE customer_id=%s AND status='PENDING'", (cid,))
        send(customer_id=cid, channel=saved.channel, body=STOP_REPLY, idempotency_key=f"{key}:stop")
        outcome = {"route": "stop"}
    elif word in START_WORDS and opted_out:
        ctx.conn.execute("UPDATE customers SET opted_out_at = NULL WHERE id=%s", (cid,))
        send(customer_id=cid, channel=saved.channel, body=START_REPLY, idempotency_key=f"{key}:start")
        outcome = {"route": "start"}
    elif opted_out:
        outcome = {"route": "opted_out_ignored"}  # they said STOP: we do not answer
    elif (match := emergency.detect(body)) is not None:
        emergency.escalate(ctx, cid, saved.inbound_id, match)
        outcome = {"route": "emergency", "rule": match.rule, "llm_calls": 0}
    elif status == "HANDOFF":
        # A person owns this conversation now. One holding text per hour, so the customer is not
        # left in silence, but the agent does not talk over the human.
        send(
            customer_id=cid, channel=saved.channel, idempotency_key=f"handoff-hold:{cid}:{ctx.now():%Y%m%d%H}",
            body="A team member has your request and will text you back shortly.",
        )
        outcome = {"route": "paused_handoff"}
    elif status == "EMERGENCY":
        # Agent stays paused until a human clears it; a person is already working on it.
        outcome = {"route": "paused_emergency"}
    else:
        match = emergency.classifier_says_emergency(body) if ctx.settings.emergency_classifier else None
        if match is not None:
            emergency.escalate(ctx, cid, saved.inbound_id, match)
            outcome = {"route": "emergency", "rule": match.rule, "detected_by": "classifier"}
        elif agent_fn is None:
            outcome = {"route": "no_agent"}
        else:
            reply = agent_fn(ctx, Caller("customer", cid), saved.inbound_id, body)
            if reply:  # always to the verified sender: there is no "to" parameter anywhere
                send(customer_id=cid, channel=saved.channel, body=reply, idempotency_key=f"{key}:reply")
            outcome = {"route": "agent", "replied": bool(reply)}
            # Enquiry from someone who has not booked: start the Day 0/1/3/7 series (idempotent).
            now_status = ctx.conn.execute("SELECT lead_status FROM customers WHERE id=%s", (cid,)).fetchone()
            if now_status and now_status[0] != "BOOKED":
                followups.schedule_lead_followups(ctx, cid, ctx.now())

    ctx.conn.execute("UPDATE inbound_messages SET processed_at = now() WHERE id=%s", (saved.inbound_id,))
    ctx.conn.commit()
    logger.info("inbound processed", extra={"route": outcome["route"], "customer_id": cid})
    return outcome
