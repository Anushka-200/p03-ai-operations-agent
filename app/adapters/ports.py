"""The interfaces our tools talk to. A fake and a real adapter implement the same one.

Why: tests and evals must run on fakes (no cost, no real messages), and swapping Google Calendar in
later (Phase 4) must not change a single tool. The contract tests run against both.
"""

from datetime import datetime
from typing import Any, Protocol


class AdapterUnavailable(RuntimeError):
    """The external system is down or timing out. Tools turn this into a recoverable error."""


class CalendarPort(Protocol):
    def busy_intervals(
        self, technician_id: int, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Busy blocks that are NOT our own bookings (our bookings are read from Postgres)."""

    def create_event(
        self, booking_id: int, technician_id: int, start: datetime, end: datetime, title: str
    ) -> str: ...

    def delete_event(self, event_id: str) -> None: ...


class CrmPort(Protocol):
    def upsert_contact(self, customer: dict[str, Any]) -> str: ...

    def upsert_deal(self, crm_contact_id: str, booking: dict[str, Any]) -> str: ...


class MessagingPort(Protocol):
    def send(
        self,
        *,
        customer_id: int | None,
        channel: str,
        body: str,
        idempotency_key: str,
        recipient: str | None = None,
    ) -> int | None:
        """Queue a message. The same idempotency_key twice sends it once.

        recipient=None means the customer; otherwise a technician/owner address (pages and alerts)."""
