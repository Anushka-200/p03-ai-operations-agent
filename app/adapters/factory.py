"""Chooses fake or real adapters from settings. The ONE place that decides, so the API, the scheduler and
the Telegram bot cannot disagree. Default is all fakes; a real adapter needs its backend switched on in
.env AND its credentials. Real writes still honour DRY_RUN (they log instead of writing)."""

import psycopg

from app.adapters.fakes import FakeCalendar, FakeCrm, OutboxMessaging
from app.adapters.google_calendar import GoogleCalendar, service_account_token_fn
from app.adapters.hubspot import HubSpotCrm
from app.adapters.ports import CalendarPort, CrmPort
from app.settings import Settings
from app.tools.types import ToolContext

# Built once per process: the breaker must remember failures across requests, and the HTTP client reuses connections.
_real: dict[str, object] = {}


def build_calendar(conn: psycopg.Connection, s: Settings) -> CalendarPort:
    if s.calendar_backend == "google":
        if "calendar" not in _real:
            _real["calendar"] = GoogleCalendar(
                s.google_calendar_id, service_account_token_fn(s.google_credentials_file), dry_run=s.dry_run
            )
        return _real["calendar"]  # type: ignore[return-value]
    return FakeCalendar(conn)


def build_crm(s: Settings) -> CrmPort:
    if s.crm_backend == "hubspot":
        if "crm" not in _real:
            _real["crm"] = HubSpotCrm(s.hubspot_token.get_secret_value(), dry_run=s.dry_run)
        return _real["crm"]  # type: ignore[return-value]
    return FakeCrm()


def make_context(conn: psycopg.Connection, s: Settings) -> ToolContext:
    return ToolContext(
        conn=conn, calendar=build_calendar(conn, s), crm=build_crm(s),
        messaging=OutboxMessaging(conn, dry_run=s.dry_run), settings=s,
    )  # fmt: skip
