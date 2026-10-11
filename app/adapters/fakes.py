"""Fake adapters: in-memory or Postgres-backed stand-ins used by tests, evals and dry-run demos.

Each has a `fail` switch so scenarios can simulate an outage (calendar down, CRM down).
"""

from itertools import count
from typing import Any

import psycopg

from app.adapters.ports import AdapterUnavailable


class FakeCalendar:
    """The calendar lives in the calendar_events table, so state survives and tests can inspect it."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise AdapterUnavailable("fake calendar is down")

    def busy_intervals(self, technician_id, start, end):
        self._check()
        rows = self.conn.execute(
            "SELECT start_at, end_at FROM calendar_events"
            " WHERE technician_id = %s AND booking_id IS NULL AND start_at < %s AND end_at > %s",
            (technician_id, end, start),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def create_event(self, booking_id, technician_id, start, end, title):
        self._check()
        row = self.conn.execute(
            "INSERT INTO calendar_events (technician_id, start_at, end_at, title, booking_id)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (technician_id, start, end, title, booking_id),
        ).fetchone()
        return f"fake-{row[0]}"

    def delete_event(self, event_id):
        self._check()
        self.conn.execute("DELETE FROM calendar_events WHERE id = %s", (int(event_id.split("-")[1]),))


class FakeCrm:
    def __init__(self) -> None:
        self.fail = False
        self.contacts: dict[str, dict[str, Any]] = {}
        self.deals: dict[str, dict[str, Any]] = {}
        self._ids = count(1)

    def _check(self) -> None:
        if self.fail:
            raise AdapterUnavailable("fake CRM is down")

    def upsert_contact(self, customer):
        self._check()
        key = f"crm-c-{customer['id']}"  # same customer twice = same contact (upsert)
        self.contacts[key] = {k: v for k, v in customer.items() if k != "phone"}
        return key

    def upsert_deal(self, crm_contact_id, booking):
        self._check()
        key = f"crm-d-{booking['id']}"
        self.deals[key] = {"contact": crm_contact_id, **booking}
        return key


class OutboxMessaging:
    """Writes every message to outbound_messages. In dry-run it is recorded but never sent."""

    def __init__(self, conn: psycopg.Connection, dry_run: bool = True) -> None:
        self.conn = conn
        self.dry_run = dry_run

    def send(self, *, customer_id, channel, body, idempotency_key, recipient=None):
        status = "DRY_RUN" if self.dry_run else "QUEUED"
        row = self.conn.execute(
            "INSERT INTO outbound_messages (customer_id, channel, body, dry_run, status, idempotency_key, recipient)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
            (customer_id, channel, body, self.dry_run, status, idempotency_key, recipient),
        ).fetchone()
        return row[0] if row else None
