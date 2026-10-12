"""Google Calendar adapter (service account, one test calendar).

Reading busy time is always allowed. Creating/deleting events is a real write, so with dry_run=True it
only logs and returns a `dryrun-` id. Events we create carry our booking id in a private property: that is
how `busy_intervals` tells OUR events (already counted from Postgres) from other people's events
(which block time). In this simple setup every non-P03 event on the calendar blocks all technicians.
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from typing import Any

import httpx

from app.adapters.http import call
from app.adapters.ports import AdapterUnavailable
from app.adapters.resilience import CircuitBreaker, retry
from app.availability import BUSINESS_TZ

logger = logging.getLogger(__name__)
API = "https://www.googleapis.com/calendar/v3/calendars"
SCOPE = "https://www.googleapis.com/auth/calendar"


def service_account_token_fn(credentials_file: str) -> Callable[[], str]:
    """Builds a function returning a fresh OAuth token from the service-account JSON file."""

    def token() -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(credentials_file, scopes=[SCOPE])
        if not creds.valid:
            try:
                creds.refresh(Request())
            except Exception as exc:  # network or auth failure: never print the library's message
                raise AdapterUnavailable(f"google auth: {type(exc).__name__}") from None
        return str(creds.token)

    return token


def _parse(edge: dict[str, str], end: bool = False) -> datetime:
    if "dateTime" in edge:
        return datetime.fromisoformat(edge["dateTime"].replace("Z", "+00:00"))
    d = date.fromisoformat(edge["date"])  # all-day event: spans local midnight to midnight
    return datetime.combine(d, time.min, tzinfo=BUSINESS_TZ).astimezone(UTC)


class GoogleCalendar:
    def __init__(
        self,
        calendar_id: str,
        token_fn: Callable[[], str],
        dry_run: bool = True,
        client: httpx.Client | None = None,
        breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.url = f"{API}/{calendar_id}/events"
        self.token_fn, self.dry_run = token_fn, dry_run
        self.client = client or httpx.Client()
        self.breaker = breaker or CircuitBreaker("google_calendar")
        self._retry_kw: dict[str, Any] = {"sleep": sleep} if sleep else {}

    def _req(self, method: str, url: str, safe_to_retry: bool, **kw: Any) -> httpx.Response:
        def once() -> httpx.Response:
            headers = {"Authorization": f"Bearer {self.token_fn()}"}
            return call(self.client, method, url, name="google_calendar", headers=headers, **kw)

        def guarded() -> httpx.Response:
            return self.breaker.call(once)

        return retry(guarded, **self._retry_kw) if safe_to_retry else guarded()

    def busy_intervals(
        self, technician_id: int, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        resp = self._req(
            "GET", self.url, True, ok=(200,),
            params={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true", "maxResults": 250},
        )  # fmt: skip
        out = []
        for ev in resp.json().get("items", []):
            if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
                continue  # cancelled, or marked "free"
            if "p03_booking_id" in ev.get("extendedProperties", {}).get("private", {}):
                continue  # ours: Postgres already knows it
            out.append((_parse(ev["start"]), _parse(ev["end"])))
        return out

    def create_event(
        self, booking_id: int, technician_id: int, start: datetime, end: datetime, title: str
    ) -> str:
        # Deterministic id (a-v and 0-9 only): retrying the same booking+time cannot create a second event.
        event_id = f"p03b{booking_id}t{int(start.timestamp())}"
        if self.dry_run:
            logger.info("calendar dry-run: would create event", extra={"booking_id": booking_id})
            return f"dryrun-{event_id}"
        body = {
            "id": event_id, "summary": title,
            "start": {"dateTime": start.astimezone(UTC).isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end.astimezone(UTC).isoformat(), "timeZone": "UTC"},
            "extendedProperties": {"private": {"p03_booking_id": str(booking_id), "p03_tech": str(technician_id)}},
        }  # fmt: skip
        # 409 = this id already exists = an earlier attempt succeeded: treat as done.
        self._req("POST", self.url, True, ok=(200, 201), tolerate=(409,), json=body)
        return event_id

    def delete_event(self, event_id: str) -> None:
        if self.dry_run or event_id.startswith("dryrun-"):
            return
        self._req(
            "DELETE", f"{self.url}/{event_id}", True, ok=(200, 204), tolerate=(404, 410)
        )  # already gone = fine
