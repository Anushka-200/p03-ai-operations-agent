"""Schema tests run against the real Postgres from docker compose (skipped if it is not up).

They prove the guarantees the database itself gives, independent of any Python code.
"""

import psycopg
import pytest

from app.db.migrate import apply_migrations
from app.settings import get_settings


def _connect(url: str):
    try:
        return psycopg.connect(url, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("Postgres is not running (make up)")


@pytest.fixture(scope="module")
def migrated():
    s = get_settings()
    _connect(s.migrator_database_url).close()
    apply_migrations()


@pytest.fixture
def app_conn(migrated):
    conn = _connect(get_settings().database_url)
    yield conn
    conn.rollback()  # every test leaves the database as it found it
    conn.close()


@pytest.fixture
def owner_conn(migrated):
    conn = _connect(get_settings().migrator_database_url)
    yield conn
    conn.rollback()
    conn.close()


def test_migrations_are_idempotent(migrated):
    assert apply_migrations() == []


def test_seed_data_present(app_conn):
    cur = app_conn.cursor()
    assert cur.execute("SELECT count(*) FROM services").fetchone()[0] >= 8
    assert (
        cur.execute("SELECT price_usd FROM services WHERE code='ac_diagnostic'").fetchone()[0] == 89
    )
    assert cur.execute("SELECT count(*) FROM technicians WHERE on_call").fetchone()[0] == 1
    assert (
        cur.execute("SELECT count(*) FROM service_area_zips WHERE zip='78704'").fetchone()[0] == 1
    )


def test_seed_is_safe_to_rerun(owner_conn):
    cur = owner_conn.cursor()
    before = cur.execute(
        "SELECT (SELECT count(*) FROM faq), (SELECT count(*) FROM technicians)"
    ).fetchone()
    from app.db.migrate import MIGRATIONS_DIR

    cur.execute((MIGRATIONS_DIR / "0002_seed.sql").read_text())
    after = cur.execute(
        "SELECT (SELECT count(*) FROM faq), (SELECT count(*) FROM technicians)"
    ).fetchone()
    assert before == after


def test_app_role_cannot_change_table_structure(app_conn):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("DROP TABLE bookings")


def test_audit_log_is_append_only_for_the_app_role(app_conn):
    app_conn.execute(
        "INSERT INTO audit_log (caller_role, tool, decision) VALUES ('customer','book_appointment','ALLOW')"
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("UPDATE audit_log SET decision='DENY'")
    app_conn.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("DELETE FROM audit_log")


def _customer(conn, n: int) -> int:
    return conn.execute(
        "INSERT INTO customers (phone, phone_hash) VALUES (%s, %s) RETURNING id",
        (f"+1512555{n:04d}", f"ph_test_{n}"),
    ).fetchone()[0]


def _book(conn, cust: int, tech: int, start: str, end: str):
    return conn.execute(
        "INSERT INTO bookings (customer_id, technician_id, service_code, start_at, end_at, price_usd)"
        " VALUES (%s, %s, 'ac_diagnostic', %s, %s, 89)",
        (cust, tech, start, end),
    )


def test_database_refuses_overlapping_bookings_for_one_technician(app_conn):
    a, b = _customer(app_conn, 9001), _customer(app_conn, 9002)
    tech = app_conn.execute("SELECT id FROM technicians ORDER BY id LIMIT 1").fetchone()[0]
    _book(app_conn, a, tech, "2030-01-10 10:00+00", "2030-01-10 11:30+00")
    with pytest.raises(psycopg.errors.ExclusionViolation):
        _book(app_conn, b, tech, "2030-01-10 11:00+00", "2030-01-10 12:30+00")


def test_back_to_back_and_other_technician_and_cancelled_are_allowed(app_conn):
    a, b = _customer(app_conn, 9003), _customer(app_conn, 9004)
    t1, t2 = [
        r[0] for r in app_conn.execute("SELECT id FROM technicians ORDER BY id LIMIT 2").fetchall()
    ]
    _book(app_conn, a, t1, "2030-01-11 10:00+00", "2030-01-11 11:30+00")
    _book(
        app_conn, b, t1, "2030-01-11 11:30+00", "2030-01-11 13:00+00"
    )  # starts exactly when the first ends
    _book(
        app_conn, b, t2, "2030-01-11 10:00+00", "2030-01-11 11:30+00"
    )  # same time, other technician
    app_conn.execute(
        "UPDATE bookings SET status='CANCELLED' WHERE customer_id=%s AND technician_id=%s", (a, t1)
    )
    _book(
        app_conn, b, t1, "2030-01-11 10:00+00", "2030-01-11 11:30+00"
    )  # a cancelled slot is free again


def test_duplicate_twilio_message_sid_is_rejected(app_conn):
    app_conn.execute(
        "INSERT INTO inbound_messages (message_sid, channel, body) VALUES ('SMtest1','sms','hi')"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        app_conn.execute(
            "INSERT INTO inbound_messages (message_sid, channel, body) VALUES ('SMtest1','sms','hi')"
        )


def test_same_followup_cannot_be_scheduled_twice(app_conn):
    c = _customer(app_conn, 9005)
    q = "INSERT INTO followups (customer_id, rule, due_at) VALUES (%s, 'day3', now())"
    app_conn.execute(q, (c,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        app_conn.execute(q, (c,))
