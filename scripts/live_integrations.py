"""One live check per integration. Nothing runs without an explicit name; writes need --write.

  uv run python -m scripts.live_integrations google          # read-only: lists busy blocks tomorrow
  uv run python -m scripts.live_integrations google --write  # also creates, then deletes, ONE test event
  uv run python -m scripts.live_integrations hubspot --write # upserts ONE test contact + deal (twice, to prove no duplicate)
  uv run python -m scripts.live_integrations twilio --write  # sends ONE message to the first TWILIO_ALLOWED_TO number

It prints ids and counts only, never tokens or phone numbers. Run each only after the owner approved it.
"""

import sys
from datetime import UTC, datetime, timedelta

import psycopg

from app.adapters.google_calendar import GoogleCalendar, service_account_token_fn
from app.adapters.hubspot import HubSpotCrm
from app.adapters.twilio_sender import TwilioSender, deliver_outbox
from app.settings import get_settings


def google(write: bool) -> None:
    s = get_settings()
    cal = GoogleCalendar(s.google_calendar_id, service_account_token_fn(s.google_credentials_file), dry_run=not write)
    start = (datetime.now(UTC) + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
    print("busy blocks tomorrow (non-P03 events):", len(cal.busy_intervals(1, start - timedelta(hours=6), start + timedelta(hours=12))))
    if write:
        eid = cal.create_event(999001, 1, start, start + timedelta(hours=1), "P03 live test (safe to delete)")
        print("created event:", eid)
        again = cal.create_event(999001, 1, start, start + timedelta(hours=1), "P03 live test (safe to delete)")
        print("same booking again returns same id (no duplicate):", again == eid)
        print("busy now excludes our own event:", all(b[0] != start for b in cal.busy_intervals(1, start - timedelta(hours=1), start + timedelta(hours=2))))
        cal.delete_event(eid)
        print("deleted")


def hubspot(write: bool) -> None:
    if not write:
        print("hubspot has no read-only check here; use --write")
        return
    s = get_settings()
    crm = HubSpotCrm(s.hubspot_token.get_secret_value(), dry_run=False)
    cust = {"id": 999001, "name": "P03 Livetest", "email": None, "address": "1 Test St", "zip": "78701", "crm_contact_id": None, "phone": "+15125550199"}
    # Like the app: the first call stores the id; the second call passes the stored id back (HubSpot's search
    # index lags a few seconds, so a search right after creating would miss the record).
    a = crm.upsert_contact(cust)
    b = crm.upsert_contact({**cust, "crm_contact_id": a, "name": "P03 Livetest Renamed"})
    booking = {"id": 999001, "service": "ac_diagnostic", "start": datetime.now(UTC), "price": 89, "discount": 0, "status": "BOOKED"}
    d1 = crm.upsert_deal(a, booking)
    d2 = crm.upsert_deal(a, {**booking, "crm_deal_id": d1, "discount": 10})
    print("contact ids equal (no duplicate):", a == b, "| deal ids equal:", d1 == d2)
    print("delete the 'P03 Livetest' contact and deal in HubSpot afterwards if you want a clean account")


def twilio(write: bool) -> None:
    s = get_settings()
    allowed = [n.strip() for n in s.twilio_allowed_to.split(",") if n.strip()]
    if not write or not allowed:
        print("needs --write and TWILIO_ALLOWED_TO")
        return
    sender = TwilioSender(s.twilio_account_sid, s.twilio_auth_token.get_secret_value(), s.twilio_sms_from, s.twilio_whatsapp_from, whatsapp_content_sid=s.twilio_whatsapp_content_sid)
    sid = sender.send("whatsapp" if s.twilio_whatsapp_from and not s.twilio_sms_from else "sms", allowed[0], "CoolBreeze test: this is the P03 live check.")
    print("Twilio accepted the message, sid starts with:", sid[:2])
    _ = (psycopg, deliver_outbox)  # the outbox path is covered by tests; this checks credentials and numbers


if __name__ == "__main__":
    name, write = (sys.argv[1] if len(sys.argv) > 1 else ""), "--write" in sys.argv
    {"google": google, "hubspot": hubspot, "twilio": twilio}.get(name, lambda w: print(__doc__))(write)
