"""Follow-ups with a fake clock: the same code, any 'now' we choose, no waiting a week."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from app import followups
from app.inbound import Saved, process_inbound
from tests.helpers import NOW, TUESDAY, book, make_customer, offers

CT = ZoneInfo("America/Chicago")


def at(ctx, when):
    ctx.now = lambda: when  # the fixture's context takes an injectable clock


def statuses(ctx, cid):
    return dict(ctx.conn.execute("SELECT rule, status FROM followups WHERE customer_id=%s", (cid,)).fetchall())


def test_four_cards_are_created_once_with_the_configured_offsets(ctx):
    c = make_customer(ctx, 701)
    followups.schedule_lead_followups(ctx, c.customer_id, NOW)
    followups.schedule_lead_followups(ctx, c.customer_id, NOW)  # same customer again: no duplicates
    rows = ctx.conn.execute("SELECT rule, due_at FROM followups WHERE customer_id=%s ORDER BY due_at", (c.customer_id,)).fetchall()
    assert [r[0] for r in rows] == ["day0", "day1", "day3", "day7"]
    assert [r[1] - NOW for r in rows] == [timedelta(hours=h) for h in (1, 24, 72, 168)]


def test_nothing_is_sent_before_it_is_due_and_each_card_is_sent_once(ctx):
    c = make_customer(ctx, 702)
    followups.schedule_lead_followups(ctx, c.customer_id, NOW)  # NOW = Mon 09:00 Austin
    at(ctx, NOW + timedelta(minutes=30))
    assert followups.tick(ctx) == {"sent": 0, "skipped": 0, "deferred": 0}
    at(ctx, NOW + timedelta(hours=1, minutes=1))  # Mon 10:01: day0 is due
    assert followups.tick(ctx)["sent"] == 1
    assert followups.tick(ctx)["sent"] == 0  # a second tick must not resend
    assert statuses(ctx, c.customer_id)["day0"] == "SENT"
    n = ctx.conn.execute("SELECT count(*) FROM outbound_messages WHERE idempotency_key=%s", (f"followup:{c.customer_id}:day0",)).fetchone()[0]
    assert n == 1


def test_quiet_hours_defer_to_8am_instead_of_texting_at_night(ctx):
    c = make_customer(ctx, 703)
    followups.schedule_lead_followups(ctx, c.customer_id, NOW)
    night = (NOW + timedelta(hours=1)).astimezone(CT).replace(hour=22, minute=0)  # Mon 22:00 Austin
    at(ctx, night)
    res = followups.tick(ctx)
    assert res["sent"] == 0 and res["deferred"] >= 1
    due = ctx.conn.execute("SELECT due_at FROM followups WHERE customer_id=%s AND rule='day0'", (c.customer_id,)).fetchone()[0].astimezone(CT)
    assert (due.day, due.hour) == (night.day + 1, 8)
    at(ctx, due + timedelta(minutes=1))
    assert followups.tick(ctx)["sent"] >= 1  # sent when the window opens


def test_booking_cancels_the_lead_series_and_adds_a_reminder(ctx):
    c = make_customer(ctx, 704)
    followups.schedule_lead_followups(ctx, c.customer_id, NOW)
    book(ctx, c, offers(ctx, c)[0]["slot_id"], key="key-704-aaaa")
    st = statuses(ctx, c.customer_id)
    assert {st[r] for r in ("day0", "day1", "day3", "day7")} == {"CANCELLED"}
    assert [r for r in st if r.startswith("reminder:")] and st[[r for r in st if r.startswith("reminder:")][0]] == "PENDING"


def test_reminder_goes_out_the_day_before_and_not_if_cancelled(ctx):
    c = make_customer(ctx, 705)
    res = book(ctx, c, offers(ctx, c, day=TUESDAY)[0]["slot_id"], key="key-705-aaaa")
    bid = res["booking_id"]
    start = ctx.conn.execute("SELECT start_at FROM bookings WHERE id=%s", (bid,)).fetchone()[0]
    at(ctx, start - timedelta(hours=23))  # Mon ~10:00 Austin, inside 8-20
    assert followups.tick(ctx)["sent"] == 1
    body = ctx.conn.execute("SELECT body FROM outbound_messages WHERE idempotency_key=%s", (f"followup:{c.customer_id}:reminder:{bid}",)).fetchone()[0]
    assert "reminder" in body and "Oct 13" in body


def test_cancelled_booking_means_no_reminder(ctx):
    c = make_customer(ctx, 706)
    bid = book(ctx, c, offers(ctx, c, day=TUESDAY)[0]["slot_id"], key="key-706-aaaa")["booking_id"]
    ctx.conn.execute("UPDATE bookings SET status='CANCELLED' WHERE id=%s", (bid,))  # cancelled by any route
    at(ctx, NOW + timedelta(hours=20))
    followups.tick(ctx)
    row = ctx.conn.execute("SELECT status, skip_reason FROM followups WHERE rule=%s", (f"reminder:{bid}",)).fetchone()
    assert row == ("SKIPPED", "booking_not_active")


def test_opted_out_booked_and_handoff_customers_are_skipped(ctx):
    a, b, h = make_customer(ctx, 707), make_customer(ctx, 708), make_customer(ctx, 709)
    for cust in (a, b, h):
        followups.schedule_lead_followups(ctx, cust.customer_id, NOW)
    ctx.conn.execute("UPDATE customers SET opted_out_at=now() WHERE id=%s", (a.customer_id,))
    ctx.conn.execute("UPDATE customers SET lead_status='HANDOFF' WHERE id=%s", (h.customer_id,))
    # b booked "outside the tools" (e.g. the owner booked by phone): the re-check at send time catches it
    book(ctx, b, offers(ctx, b)[0]["slot_id"], key="key-708-aaaa")
    ctx.conn.execute("UPDATE followups SET status='PENDING' WHERE customer_id=%s AND rule='day0'", (b.customer_id,))
    at(ctx, NOW + timedelta(hours=1, minutes=1))
    followups.tick(ctx)
    for cust, why in ((a, "opted_out"), (b, "already_booked"), (h, "paused:handoff")):
        row = ctx.conn.execute("SELECT status, skip_reason FROM followups WHERE customer_id=%s AND rule='day0'", (cust.customer_id,)).fetchone()
        assert row == ("SKIPPED", why), (cust, row)


def test_enquiry_through_the_inbound_pipeline_starts_the_series_and_stop_cancels_it(ctx):
    # process_inbound commits (as it does in production), so this test cleans up for real afterwards.
    import psycopg

    from app.settings import get_settings

    c = make_customer(ctx, 710)
    ctx.conn.commit()
    try:
        _inbound_flow(ctx, c)
    finally:
        with psycopg.connect(get_settings().migrator_database_url, autocommit=True) as o:
            for t in ("outbound_messages", "followups", "inbound_messages", "audit_log"):
                o.execute(f"DELETE FROM {t} WHERE customer_id=%s", (c.customer_id,))
            o.execute("DELETE FROM customers WHERE id=%s", (c.customer_id,))


def _inbound_flow(ctx, c):
    inbound = ctx.conn.execute("INSERT INTO inbound_messages (message_sid, customer_id, channel, body) VALUES ('SM-fu-1', %s, 'sms', 'hi') RETURNING id", (c.customer_id,)).fetchone()[0]
    process_inbound(ctx, Saved(inbound, c.customer_id, "sms", False), "hi", lambda *a: "Hello!")
    assert len(statuses(ctx, c.customer_id)) == 4
    inbound2 = ctx.conn.execute("INSERT INTO inbound_messages (message_sid, customer_id, channel, body) VALUES ('SM-fu-2', %s, 'sms', 'STOP') RETURNING id", (c.customer_id,)).fetchone()[0]
    process_inbound(ctx, Saved(inbound2, c.customer_id, "sms", False), "STOP", lambda *a: "never")
    assert set(statuses(ctx, c.customer_id).values()) == {"CANCELLED"}


def test_the_scheduler_runs_followups_message_delivery_and_crm_sync():
    """Regression: process_outbox existed but nothing called it, so CRM updates queued forever."""
    from app.scheduler.main import build_scheduler

    assert {j.id for j in build_scheduler().get_jobs()} == {"followup_tick", "twilio_delivery", "crm_sync"}
