"""Edge cases found while hardening: rescheduling into taken slots, half-failed calendar calls,
CRM re-sync, the per-customer lock, and the owner's /cases + /resume (emergency and handoff clearing)."""

from datetime import timedelta

import psycopg
import pytest

from app import cases, emergency
from app.adapters.ports import AdapterUnavailable
from app.adapters.telegram import FakeTelegram
from app.availability import parse_slot_id
from app.concurrency import lock_conversation
from app.emergency import EmergencyMatch
from app.inbound import Saved, process_inbound
from app.owner_bot import handle_update
from app.settings import get_settings
from app.tools import decide_approval, run_tool
from tests.helpers import TUESDAY, audit_rows, book, make_customer, offers
from tests.test_owner_bot import (  # noqa: F401  (NoCommit used by the bot fixture)
    OWNER_ID,
    NoCommit,
    text_update,
)

WED, THU = TUESDAY + timedelta(days=1), TUESDAY + timedelta(days=2)


def _count(ctx, sql, *params):
    return ctx.conn.execute(sql, params).fetchone()[0]


def _booking_row(ctx, booking_id):
    return ctx.conn.execute(
        "SELECT start_at, technician_id FROM bookings WHERE id=%s", (booking_id,)
    ).fetchone()


def _resched(ctx, c, booking_id, slot_id, key="resched-edge-1"):
    return run_tool(ctx, c, "reschedule_appointment",
                    {"booking_id": booking_id, "new_slot_id": slot_id, "idempotency_key": key})  # fmt: skip


# ---------- rescheduling into a slot that is not free ----------


def test_reschedule_into_a_busy_calendar_block_is_refused_and_nothing_moves(ctx):
    maria = make_customer(ctx, 1)
    booked = book(ctx, maria, offers(ctx, maria, WED)[0]["slot_id"])
    before = _booking_row(ctx, booked["booking_id"])
    target = offers(ctx, maria, THU)[0]["slot_id"]
    _, start = parse_slot_id(target)
    for (tid,) in ctx.conn.execute("SELECT id FROM technicians WHERE active").fetchall():
        ctx.conn.execute(
            "INSERT INTO calendar_events (technician_id, start_at, end_at, title) VALUES (%s, %s, %s, 'lunch')",
            (tid, start, start + timedelta(hours=2)),
        )  # every technician is busy at the requested time (the 'Thursday 10:00' case)
    res = _resched(ctx, maria, booked["booking_id"], target)
    assert res["ok"] is False and res["error"] == "slot_unavailable"
    assert "slot_id" in res["message"]  # alternatives are offered, not a dead end
    assert _booking_row(ctx, booked["booking_id"]) == before


def test_reschedule_into_a_slot_another_customer_just_took_is_refused(ctx):
    maria, dev = make_customer(ctx, 1), make_customer(ctx, 2)
    mine = book(ctx, maria, offers(ctx, maria, WED)[0]["slot_id"])
    taken = offers(ctx, dev, THU)[0]["slot_id"]
    assert book(ctx, dev, taken, key="key-dev-12345")["ok"]
    before = _booking_row(ctx, mine["booking_id"])
    res = _resched(ctx, maria, mine["booking_id"], taken)  # same technician, same time as Dev
    assert res["ok"] is False and res["error"] == "slot_unavailable"
    assert _booking_row(ctx, mine["booking_id"]) == before
    assert (
        _count(
            ctx,
            "SELECT count(*) FROM bookings a JOIN bookings b ON a.id < b.id AND a.technician_id = b.technician_id"
            " AND a.status='BOOKED' AND b.status='BOOKED' AND tstzrange(a.start_at,a.end_at) && tstzrange(b.start_at,b.end_at)",
        )
        == 0
    )


def test_the_database_itself_refuses_an_overlap_even_if_the_code_check_is_skipped(ctx):
    maria, dev = make_customer(ctx, 1), make_customer(ctx, 2)
    a = book(ctx, maria, offers(ctx, maria, WED)[0]["slot_id"])
    b = book(ctx, dev, offers(ctx, dev, THU)[0]["slot_id"], key="key-dev-12345")
    with pytest.raises(psycopg.errors.ExclusionViolation), ctx.conn.transaction():
        ctx.conn.execute(
            "UPDATE bookings SET technician_id=(SELECT technician_id FROM bookings WHERE id=%s),"
            " start_at=(SELECT start_at FROM bookings WHERE id=%s), end_at=(SELECT end_at FROM bookings WHERE id=%s) WHERE id=%s",
            (b["booking_id"], b["booking_id"], b["booking_id"], a["booking_id"]),
        )


def test_reschedule_when_creating_the_new_event_fails_keeps_the_old_booking_and_event(ctx):
    maria = make_customer(ctx, 1)
    booked = book(ctx, maria, offers(ctx, maria, WED)[0]["slot_id"])
    before = _booking_row(ctx, booked["booking_id"])
    target = offers(ctx, maria, THU)[0]["slot_id"]

    def boom(*a, **k):
        raise AdapterUnavailable("down")

    ctx.calendar.create_event = boom
    res = _resched(ctx, maria, booked["booking_id"], target)
    assert res["error"] == "calendar_unavailable"
    assert _booking_row(ctx, booked["booking_id"]) == before
    assert _count(ctx, "SELECT count(*) FROM calendar_events WHERE booking_id=%s", booked["booking_id"]) == 1


def test_reschedule_when_deleting_the_old_event_fails_leaves_one_event_not_two(ctx):
    maria = make_customer(ctx, 1)
    booked = book(ctx, maria, offers(ctx, maria, WED)[0]["slot_id"])
    before = _booking_row(ctx, booked["booking_id"])
    target = offers(ctx, maria, THU)[0]["slot_id"]
    real_delete = ctx.calendar.delete_event
    old_event = ctx.conn.execute(
        "SELECT calendar_event_id FROM bookings WHERE id=%s", (booked["booking_id"],)
    ).fetchone()[0]

    def flaky(event_id):
        if event_id == old_event:
            raise AdapterUnavailable("down")
        real_delete(event_id)

    ctx.calendar.delete_event = flaky
    res = _resched(ctx, maria, booked["booking_id"], target)
    assert res["error"] == "calendar_unavailable"
    assert _booking_row(ctx, booked["booking_id"]) == before  # the booking did not move
    assert _count(ctx, "SELECT count(*) FROM calendar_events WHERE booking_id=%s", booked["booking_id"]) == 1


# ---------- the CRM follows every change to a booking ----------


def _deal_syncs(ctx, booking_id):
    return _count(
        ctx,
        "SELECT count(*) FROM crm_outbox WHERE kind='upsert_deal' AND (payload->>'booking_id')::int = %s",
        booking_id,
    )


def test_reschedule_cancel_and_discount_each_queue_a_crm_deal_update(ctx):
    maria = make_customer(ctx, 1)
    booked = book(ctx, maria, offers(ctx, maria, WED)[0]["slot_id"])
    bid = booked["booking_id"]
    assert _deal_syncs(ctx, bid) == 1  # the booking itself
    assert _resched(ctx, maria, bid, offers(ctx, maria, THU)[0]["slot_id"])["ok"]
    assert _deal_syncs(ctx, bid) == 2
    ask = run_tool(ctx, maria, "request_discount", {"booking_id": bid, "pct": 10, "reason": "loyal customer"})
    assert ask["error"] == "needs_approval"
    assert decide_approval(ctx, ask["approval_id"], True, "owner:test")["ok"]
    assert _deal_syncs(ctx, bid) == 3
    res = run_tool(ctx, maria, "cancel_appointment", {"booking_id": bid, "reason": "plans changed"})
    assert res["ok"] and _deal_syncs(ctx, bid) == 4


# ---------- one conversation at a time per customer ----------


def test_a_second_connection_waits_for_the_customers_conversation_lock(ctx):
    other = psycopg.connect(get_settings().database_url)
    try:
        lock_conversation(ctx.conn, 777)  # message 1 is being handled (transaction open)
        other.execute("SET lock_timeout = '300ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            lock_conversation(other, 777)  # message 2 from the same customer must wait
        other.rollback()
        lock_conversation(other, 778)  # a different customer does not wait at all
        other.rollback()
        ctx.conn.rollback()  # message 1 finished: the lock is released by Postgres itself
        lock_conversation(other, 777)
    finally:
        other.rollback()
        other.close()


# ---------- emergencies carry a number to call; /cases and /resume ----------


def _escalate(ctx, c):
    ctx.conn.execute(
        "UPDATE technicians SET on_call = true WHERE id = (SELECT min(id) FROM technicians WHERE active)"
    )
    ib = ctx.conn.execute(
        "INSERT INTO inbound_messages (message_sid, customer_id, channel, body) VALUES ('SM-edge-1', %s, 'sms', 'gas smell') RETURNING id",
        (c.customer_id,),
    ).fetchone()[0]
    emergency.escalate(ctx, c.customer_id, ib, EmergencyMatch("gas_leak", "gas"))


def test_the_page_and_owner_alert_carry_the_phone_but_the_audit_row_does_not(ctx):
    c = make_customer(ctx, 5)
    _escalate(ctx, c)
    phone = "+15125550005"
    texts = [r[0] for r in ctx.conn.execute("SELECT body FROM outbound_messages WHERE customer_id IS NULL")]
    assert any(phone in t and "EMERGENCY" in t for t in texts)
    assert any(phone in t and "Emergency escalated" in t for t in texts)
    assert all(
        phone not in str(r)
        for r in ctx.conn.execute("SELECT args FROM audit_log WHERE tool='emergency_escalation'").fetchall()
    )


def test_cases_lists_open_emergencies_and_resume_hands_the_customer_back(ctx):
    c = make_customer(ctx, 5)
    _escalate(ctx, c)
    open_cases = cases.list_cases(ctx)
    assert [x["customer_id"] for x in open_cases] == [c.customer_id] and open_cases[0][
        "status"
    ] == "EMERGENCY"
    assert "+15125550005" in cases.describe_cases(open_cases) and "/resume" in cases.describe_cases(
        open_cases
    )
    done = cases.clear_case(ctx, c.customer_id, "owner:test")
    assert done == {"ok": True, "status": "QUALIFYING", "was": "EMERGENCY"}
    assert cases.list_cases(ctx) == []
    assert (
        _count(
            ctx,
            "SELECT count(*) FROM emergencies WHERE customer_id=%s AND cleared_at IS NOT NULL AND cleared_by='owner:test'",
            c.customer_id,
        )
        == 1
    )
    assert any(r[0] == "clear_case" and r[1] == "ALLOW" for r in audit_rows(ctx))


def test_resume_of_a_customer_with_a_visit_returns_to_booked_and_a_normal_customer_is_refused(ctx):
    maria = make_customer(ctx, 1)
    assert book(ctx, maria, offers(ctx, maria, WED)[0]["slot_id"])["ok"]
    ctx.conn.execute("UPDATE customers SET lead_status='HANDOFF' WHERE id=%s", (maria.customer_id,))
    assert cases.clear_case(ctx, maria.customer_id, "owner:test")["status"] == "BOOKED"
    again = cases.clear_case(ctx, maria.customer_id, "owner:test")
    assert again["ok"] is False and "not waiting" in again["message"]
    assert cases.clear_case(ctx, 987654, "owner:test")["ok"] is False


@pytest.fixture
def bot(ctx):
    ctx.settings = ctx.settings.model_copy(update={"telegram_owner_id": OWNER_ID})
    ctx.conn = NoCommit(ctx.conn)
    return FakeTelegram()


def test_owner_commands_cases_and_resume_work_in_telegram_and_strangers_cannot_use_them(ctx, bot):
    c = make_customer(ctx, 6)
    ctx.conn.execute("UPDATE customers SET lead_status='HANDOFF' WHERE id=%s", (c.customer_id,))
    assert (
        handle_update(ctx, bot, None, text_update(999, f"/resume {c.customer_id}"), []) == "ignored_not_owner"
    )
    assert handle_update(ctx, bot, None, text_update(999, "/cases"), []) == "ignored_not_owner"
    assert bot.sent == []
    assert handle_update(ctx, bot, None, text_update(OWNER_ID, "/cases"), []) == "cases"
    assert f"#{c.customer_id}" in bot.sent[-1]["text"]
    assert handle_update(ctx, bot, None, text_update(OWNER_ID, "/resume"), []) == "resume_usage"
    assert handle_update(ctx, bot, None, text_update(OWNER_ID, "/resume abc"), []) == "resume_usage"
    assert handle_update(ctx, bot, None, text_update(OWNER_ID, f"/resume {c.customer_id}"), []) == "resumed"
    assert "back with the agent" in bot.sent[-1]["text"]
    assert (
        handle_update(ctx, bot, None, text_update(OWNER_ID, f"/resume {c.customer_id}"), [])
        == "resume_failed"
    )


def test_after_resume_the_agent_answers_that_customer_again(ctx):
    ctx.conn = NoCommit(ctx.conn)  # process_inbound commits like production; here commit must do nothing
    c = make_customer(ctx, 7)
    ctx.conn.execute("UPDATE customers SET lead_status='HANDOFF' WHERE id=%s", (c.customer_id,))
    seen = []

    def agent(ctx_, caller, inbound_id, body):
        seen.append(body)
        return "Hi again"

    def inbound(sid):
        i = ctx.conn.execute(
            "INSERT INTO inbound_messages (message_sid, customer_id, channel, body) VALUES (%s, %s, 'sms', 'hello') RETURNING id",
            (sid, c.customer_id),
        ).fetchone()[0]
        return Saved(i, c.customer_id, "sms", duplicate=False)

    assert process_inbound(ctx, inbound("SM-h1"), "hello", agent)["route"] == "paused_handoff" and seen == []
    cases.clear_case(ctx, c.customer_id, "owner:test")
    assert process_inbound(ctx, inbound("SM-h2"), "hello", agent)["route"] == "agent" and seen == ["hello"]


def test_a_contact_change_reaches_the_crm_even_when_the_contact_id_is_already_known(ctx):
    """Regression: crm_sync skipped the contact update once crm_contact_id was stored."""
    maria = make_customer(ctx, 1)
    run_tool(ctx, maria, "update_my_contact", {"name": "Maria Lopez"})
    assert crm_sync_run(ctx)["done"] >= 1
    run_tool(ctx, maria, "update_my_contact", {"address": "9 New Rd"})
    assert crm_sync_run(ctx)["done"] >= 1
    key = f"crm-c-{maria.customer_id}"
    assert ctx.crm.contacts[key]["address"] == "9 New Rd"


def crm_sync_run(ctx):
    from app import crm_sync

    return crm_sync.process_outbox(ctx)


# ---------- one pending change per booking ----------


def _pending(ctx, cid):
    return ctx.conn.execute(
        "SELECT kind, status FROM approvals WHERE customer_id=%s ORDER BY id", (cid,)
    ).fetchall()


def test_a_newer_change_request_for_the_same_booking_supersedes_the_older_pending_one(ctx):
    maria = make_customer(ctx, 1)
    booked = book(
        ctx, maria, offers(ctx, maria, TUESDAY)[0]["slot_id"]
    )  # < 24 h ahead: changes need approval
    bid = booked["booking_id"]
    first = run_tool(ctx, maria, "cancel_appointment", {"booking_id": bid, "reason": "plans changed"})
    assert first["error"] == "needs_approval"
    new_slot = offers(ctx, maria, TUESDAY + timedelta(days=2))[0]["slot_id"]
    second = run_tool(
        ctx,
        maria,
        "reschedule_appointment",
        {"booking_id": bid, "new_slot_id": new_slot, "idempotency_key": "resched-sup-1"},
    )
    assert second["error"] == "needs_approval" and second["approval_id"] != first["approval_id"]
    assert _pending(ctx, maria.customer_id) == [
        ("cancel_appointment", "EXPIRED"),
        ("reschedule_appointment", "PENDING"),
    ]
    stale = decide_approval(ctx, first["approval_id"], True, "owner:test")  # a tap on the old card
    assert stale["ok"] is False and stale["error"] == "already_decided"
    assert ctx.conn.execute("SELECT status FROM bookings WHERE id=%s", (bid,)).fetchone()[0] == "BOOKED"


def test_the_same_request_twice_still_reuses_one_approval_and_other_customers_are_untouched(ctx):
    maria, dev = make_customer(ctx, 1), make_customer(ctx, 2)
    a = book(ctx, maria, offers(ctx, maria, TUESDAY)[0]["slot_id"])
    b = book(
        ctx, dev, offers(ctx, dev, TUESDAY)[0]["slot_id"], key="key-dev-12345"
    )  # same hour, other technician
    args = {"booking_id": a["booking_id"], "reason": "plans changed"}
    first, again = (
        run_tool(ctx, maria, "cancel_appointment", args),
        run_tool(ctx, maria, "cancel_appointment", args),
    )
    assert first["approval_id"] == again["approval_id"]
    assert (
        run_tool(ctx, dev, "cancel_appointment", {"booking_id": b["booking_id"], "reason": "moving away"})[
            "error"
        ]
        == "needs_approval"
    )
    assert [s for _, s in _pending(ctx, maria.customer_id)] == ["PENDING"]
    assert [s for _, s in _pending(ctx, dev.customer_id)] == [
        "PENDING"
    ]  # one customer's request never touches another's
