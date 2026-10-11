"""Real concurrency: separate database connections racing for the same slot.

These commit for real (the race needs separate transactions), then clean up as the migrator role.
"""

import threading
from datetime import timedelta

import psycopg
import pytest

from app.adapters.fakes import FakeCalendar, FakeCrm, OutboxMessaging
from app.settings import get_settings
from app.tools import Caller, ToolContext, run_tool
from tests.helpers import NOW, TUESDAY

SERVICE = "ac_diagnostic"


def _ctx() -> ToolContext:
    s = get_settings()
    try:
        conn = psycopg.connect(s.database_url, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("Postgres is not running (make up)")
    return ToolContext(conn, FakeCalendar(conn), FakeCrm(), OutboxMessaging(conn, True), s, now=lambda: NOW)


@pytest.fixture
def two_customers():
    setup = _ctx()
    ids = []
    for n in (8001, 8002):
        ids.append(
            setup.conn.execute(
                "INSERT INTO customers (phone, phone_hash, name, address, zip) VALUES (%s, %s, 'T', '1 A St', '78704')"
                " RETURNING id",
                (f"+1512555{n}", f"ph_race{n}"),
            ).fetchone()[0]
        )
    yield ids
    owner = psycopg.connect(get_settings().migrator_database_url, autocommit=True)
    owner.execute(
        "DELETE FROM calendar_events WHERE booking_id IN (SELECT id FROM bookings WHERE customer_id = ANY(%s))",
        (ids,),
    )
    owner.execute("DELETE FROM crm_outbox WHERE customer_id = ANY(%s)", (ids,))
    owner.execute("DELETE FROM followups WHERE customer_id = ANY(%s)", (ids,))
    owner.execute("DELETE FROM bookings WHERE customer_id = ANY(%s)", (ids,))
    owner.execute("DELETE FROM idempotency_keys WHERE key LIKE ANY(%s)", ([f"book:{i}:%" for i in ids],))
    owner.execute("DELETE FROM audit_log WHERE customer_id = ANY(%s)", (ids,))
    owner.execute("DELETE FROM customers WHERE id = ANY(%s)", (ids,))
    owner.close()
    setup.conn.close()


def _first_slot(ctx: ToolContext, caller: Caller):
    res = run_tool(
        ctx, caller, "find_slots", {"service_code": SERVICE, "date_from": str(TUESDAY + timedelta(days=1))}
    )
    return res["slots"][0]["slot_id"]


def _race(jobs):
    barrier = threading.Barrier(len(jobs))
    results = [None] * len(jobs)

    def go(i, job):
        barrier.wait()  # release all threads at the same moment
        results[i] = job()

    threads = [threading.Thread(target=go, args=(i, j)) for i, j in enumerate(jobs)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results


def test_two_customers_racing_for_one_technician_slot_never_double_book(two_customers):
    a, b = (Caller("customer", i) for i in two_customers)
    ctx_a, ctx_b = _ctx(), _ctx()
    slot_id = _first_slot(ctx_a, a)
    tech = int(slot_id.split("-")[0])
    # Make that technician the only one free by blocking the others for the whole week.
    owner = psycopg.connect(get_settings().migrator_database_url, autocommit=True)
    owner.execute(
        "INSERT INTO calendar_events (technician_id, start_at, end_at, title) SELECT id, %s, %s, 'race-test' "
        "FROM technicians WHERE id <> %s RETURNING id",
        (NOW, NOW + timedelta(days=30), tech),
    )
    try:
        results = _race(
            [
                lambda: run_tool(
                    ctx_a,
                    a,
                    "book_appointment",
                    {"slot_id": slot_id, "service_code": SERVICE, "idempotency_key": "race-key-a1"},
                ),
                lambda: run_tool(
                    ctx_b,
                    b,
                    "book_appointment",
                    {"slot_id": slot_id, "service_code": SERVICE, "idempotency_key": "race-key-b1"},
                ),
            ]
        )
        winners = [r for r in results if r["ok"]]
        losers = [r for r in results if not r["ok"]]
        assert len(winners) == 1 and len(losers) == 1, results
        assert losers[0]["error"] == "slot_unavailable"
        n = owner.execute(
            "SELECT count(*) FROM bookings WHERE technician_id = %s AND customer_id = ANY(%s)",
            (tech, two_customers),
        ).fetchone()[0]
        assert n == 1
    finally:
        owner.execute("DELETE FROM calendar_events WHERE title = 'race-test'")
        owner.close()
        ctx_a.conn.close()
        ctx_b.conn.close()


def test_the_same_request_sent_twice_at_once_books_once(two_customers):
    a = Caller("customer", two_customers[0])
    ctx1, ctx2 = _ctx(), _ctx()
    slot_id = _first_slot(ctx1, a)
    args = {"slot_id": slot_id, "service_code": SERVICE, "idempotency_key": "same-key-123"}
    try:
        results = _race(
            [
                lambda: run_tool(ctx1, a, "book_appointment", args),
                lambda: run_tool(ctx2, a, "book_appointment", args),
            ]
        )
        assert all(r["ok"] for r in results), results
        assert results[0]["booking_id"] == results[1]["booking_id"]
        n = ctx1.conn.execute(
            "SELECT count(*) FROM bookings WHERE customer_id = %s", (a.customer_id,)
        ).fetchone()[0]
        assert n == 1
    finally:
        ctx1.conn.close()
        ctx2.conn.close()
