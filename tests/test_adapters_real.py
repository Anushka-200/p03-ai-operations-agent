"""Contract tests: the fake and the real adapter must behave the same from the tools' point of view.

The real adapters talk to tiny in-memory servers (httpx.MockTransport) that imitate the parts of the Google,
HubSpot and Twilio APIs we use. That proves OUR code (ids, upserts, idempotence, dry-run, breaker, retries,
no secrets in errors). It cannot prove the real services accept our requests: that is the live run per
integration, which needs your credentials and your OK."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.adapters.fakes import FakeCalendar, FakeCrm
from app.adapters.google_calendar import GoogleCalendar
from app.adapters.http import AdapterRejected
from app.adapters.hubspot import HubSpotCrm
from app.adapters.ports import AdapterUnavailable
from app.adapters.resilience import CircuitBreaker, retry
from app.adapters.twilio_sender import TwilioSender, deliver_outbox
from tests.helpers import make_customer
from tests.test_owner_bot import NoCommit

START = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
END = START + timedelta(hours=1)
SECRET = "tok-SECRET-123"


# ------------------------------------------------------------------ in-memory servers

class FakeGoogle:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.down = False
        self.status_override: int | None = None

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path))
        assert req.headers["authorization"] == f"Bearer {SECRET}"
        if self.down:
            return httpx.Response(503)
        if self.status_override:
            return httpx.Response(self.status_override)
        if req.method == "GET":
            return httpx.Response(200, json={"items": list(self.events.values())})
        if req.method == "POST":
            body = json.loads(req.content)
            if body["id"] in self.events:
                return httpx.Response(409)
            self.events[body["id"]] = body
            return httpx.Response(200, json=body)
        if req.method == "DELETE":
            eid = req.url.path.rsplit("/", 1)[1]
            return httpx.Response(204) if self.events.pop(eid, None) else httpx.Response(404)
        return httpx.Response(400)


class FakeHubSpot:
    def __init__(self):
        self.objs = {"contacts": {}, "deals": {}}
        self.calls: list[tuple[str, str]] = []
        self.down = False
        self.search_blind = False  # True = HubSpot's search index lags: records just written are not found yet
        self.n = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path))
        assert req.headers["authorization"] == f"Bearer {SECRET}"
        if self.down:
            return httpx.Response(429)
        parts = req.url.path.strip("/").split("/")  # crm v3 objects <kind> [id|search]
        kind = parts[3]
        body = json.loads(req.content) if req.content else {}
        if req.method == "POST" and parts[-1] == "search":
            f = body["filterGroups"][0]["filters"][0]
            hits = [{"id": i, "properties": p} for i, p in self.objs[kind].items() if p.get(f["propertyName"]) == f["value"]]
            if self.search_blind:
                hits = []
            return httpx.Response(200, json={"results": hits[:1]})
        if req.method == "POST":
            self.n += 1
            self.objs[kind][str(self.n)] = body["properties"]
            return httpx.Response(201, json={"id": str(self.n)})
        if req.method == "PATCH":
            if parts[4] not in self.objs[kind]:
                return httpx.Response(404)
            self.objs[kind][parts[4]].update(body["properties"])
            return httpx.Response(200, json={"id": parts[4]})
        return httpx.Response(400)


def google(dry_run=False, server=None, breaker=None):
    server = server or FakeGoogle()
    cal = GoogleCalendar("test-cal", lambda: SECRET, dry_run=dry_run, client=httpx.Client(transport=httpx.MockTransport(server)),
                         breaker=breaker, sleep=lambda s: None)  # fmt: skip
    return cal, server


def hubspot(dry_run=False, server=None):
    server = server or FakeHubSpot()
    return HubSpotCrm(SECRET, dry_run=dry_run, client=httpx.Client(transport=httpx.MockTransport(server)), sleep=lambda s: None), server


# ------------------------------------------------------------------ calendar contract (fake AND real)

class BookingBackedCalendar(FakeCalendar):
    """The fake stores events in a table whose booking_id is a foreign key, so a real booking row must exist."""

    def __init__(self, ctx):
        super().__init__(ctx.conn)
        self.ctx = ctx

    def create_event(self, booking_id, technician_id, start, end, title):
        c = make_customer(self.ctx, 950 + booking_id)
        self.ctx.conn.execute(
            "INSERT INTO bookings (id, customer_id, technician_id, service_code, start_at, end_at, price_usd)"
            " VALUES (%s, %s, %s, 'ac_diagnostic', %s, %s, 89)",
            (booking_id, c.customer_id, technician_id, datetime(2031, 1, booking_id, 15, tzinfo=UTC), datetime(2031, 1, booking_id, 16, tzinfo=UTC)),
        )
        return super().create_event(booking_id, technician_id, start, end, title)


@pytest.fixture(params=["fake", "google"])
def calendar_case(request, ctx):
    tech = ctx.conn.execute("SELECT id FROM technicians ORDER BY id LIMIT 1").fetchone()[0]
    if request.param == "fake":
        cal = BookingBackedCalendar(ctx)
        return cal, tech, lambda: ctx.conn.execute(
            "INSERT INTO calendar_events (technician_id, start_at, end_at, title) VALUES (%s,%s,%s,'busy')", (tech, START, END)
        ), lambda down: setattr(cal, "fail", down)  # fmt: skip
    cal, server = google()
    def external():
        server.events["ext1"] = {"id": "ext1", "start": {"dateTime": START.isoformat()}, "end": {"dateTime": END.isoformat()}}
    return cal, tech, external, lambda down: setattr(server, "down", down)


def test_calendar_contract_own_events_are_not_busy_but_outside_events_are(calendar_case):
    cal, tech, add_external, _ = calendar_case
    cal.create_event(1, tech, START, END, "AC visit")
    assert cal.busy_intervals(tech, START - timedelta(hours=1), END + timedelta(hours=1)) == []
    add_external()
    assert cal.busy_intervals(tech, START - timedelta(hours=1), END + timedelta(hours=1)) == [(START, END)]


def test_calendar_contract_delete_is_idempotent(calendar_case):
    cal, tech, _, _ = calendar_case
    event_id = cal.create_event(2, tech, START, END, "AC visit")
    cal.delete_event(event_id)
    if isinstance(cal, GoogleCalendar):
        cal.delete_event(event_id)  # already gone: no error (the fake's table delete is also a no-op)


def test_calendar_contract_outage_raises_adapter_unavailable(calendar_case):
    cal, tech, _, set_down = calendar_case
    set_down(True)
    with pytest.raises(AdapterUnavailable):
        cal.busy_intervals(tech, START, END)


# ------------------------------------------------------------------ Google specifics

def test_google_dry_run_makes_no_write_calls_and_returns_a_placeholder():
    cal, server = google(dry_run=True)
    eid = cal.create_event(3, 1, START, END, "AC visit")
    cal.delete_event(eid)
    assert eid.startswith("dryrun-") and server.calls == []


def test_google_retry_of_the_same_booking_is_a_success_not_a_duplicate():
    cal, server = google()
    a = cal.create_event(4, 1, START, END, "AC visit")
    b = cal.create_event(4, 1, START, END, "AC visit")  # server answers 409; we treat it as done
    assert a == b and len(server.events) == 1
    c = cal.create_event(4, 1, START + timedelta(days=1), END + timedelta(days=1), "moved")
    assert c != a  # a reschedule gets a new id (a deleted Google id cannot be reused)


def test_google_event_carries_our_booking_id_and_no_phone_number():
    cal, server = google()
    cal.create_event(5, 7, START, END, "AC visit")
    ev = next(iter(server.events.values()))
    assert ev["extendedProperties"]["private"] == {"p03_booking_id": "5", "p03_tech": "7"}


def test_a_503_is_retried_then_succeeds():
    cal, server = google()
    flaky = {"n": 0}
    inner = server

    def handler(req):
        flaky["n"] += 1
        return httpx.Response(503) if flaky["n"] == 1 else inner(req)

    cal.client = httpx.Client(transport=httpx.MockTransport(handler))
    cal.create_event(6, 1, START, END, "AC visit")
    assert flaky["n"] == 2 and len(server.events) == 1


def test_a_4xx_is_rejected_and_not_retried():
    cal, server = google()
    server.status_override = 403
    with pytest.raises(AdapterRejected):
        cal.create_event(7, 1, START, END, "x")
    assert len(server.calls) == 1


def test_errors_never_contain_the_token_or_the_url():
    cal, server = google()
    server.down = True
    with pytest.raises(AdapterUnavailable) as err:
        cal.busy_intervals(1, START, END)
    assert SECRET not in str(err.value) and "googleapis" not in str(err.value)


# ------------------------------------------------------------------ circuit breaker

def test_breaker_opens_after_3_failures_fails_fast_then_recovers_after_cooldown():
    now = {"t": 0.0}
    br = CircuitBreaker("x", clock=lambda: now["t"])
    cal, server = google(breaker=br)
    server.down = True
    for _ in range(3):
        with pytest.raises(AdapterUnavailable):
            cal.breaker.call(lambda: cal._req("GET", cal.url, False, ok=(200,)))
    assert br.state == "open"
    before = len(server.calls)
    with pytest.raises(AdapterUnavailable, match="temporarily disabled"):
        cal.busy_intervals(1, START, END)
    assert len(server.calls) == before  # failed fast: no HTTP at all
    now["t"] = 121
    assert br.state == "half_open"
    server.down = False
    assert cal.busy_intervals(1, START, END) == []  # the trial call succeeded
    assert br.state == "closed"


def test_a_failed_trial_call_reopens_the_breaker():
    now = {"t": 0.0}
    br = CircuitBreaker("x", threshold=1, clock=lambda: now["t"])
    with pytest.raises(AdapterUnavailable):
        br.call(lambda: (_ for _ in ()).throw(AdapterUnavailable("down")))
    now["t"] = 200
    with pytest.raises(AdapterUnavailable):
        br.call(lambda: (_ for _ in ()).throw(AdapterUnavailable("still down")))
    assert br.state == "open"


def test_retry_waits_with_growing_jittered_delays_and_gives_up():
    waits = []
    with pytest.raises(AdapterUnavailable):
        retry(lambda: (_ for _ in ()).throw(AdapterUnavailable("x")), attempts=3, base_s=1, sleep=waits.append)
    assert len(waits) == 2 and 0.5 <= waits[0] <= 1.5 and 1.0 <= waits[1] <= 3.0


# ------------------------------------------------------------------ CRM contract (fake AND real)

@pytest.fixture(params=["fake", "hubspot"])
def crm_case(request):
    if request.param == "fake":
        crm = FakeCrm()
        return crm, lambda down: setattr(crm, "fail", down)
    crm, server = hubspot()
    return crm, lambda down: setattr(server, "down", down)


CUSTOMER = {"id": 11, "name": "Maria Lopez", "email": None, "address": "1200 Oak St", "zip": "78704", "crm_contact_id": None, "phone": "+15125550111"}
BOOKING = {"id": 21, "service": "ac_diagnostic", "start": START, "price": 89, "discount": 10, "status": "BOOKED"}


def test_crm_contract_upserting_twice_never_duplicates(crm_case):
    crm, _ = crm_case
    c1 = crm.upsert_contact(CUSTOMER)
    c2 = crm.upsert_contact({**CUSTOMER, "crm_contact_id": c1})
    assert c1 == c2
    d1 = crm.upsert_deal(c1, BOOKING)
    assert crm.upsert_deal(c1, BOOKING) == d1


def test_crm_contract_outage_raises_adapter_unavailable(crm_case):
    crm, set_down = crm_case
    set_down(True)
    with pytest.raises(AdapterUnavailable):
        crm.upsert_contact(CUSTOMER)


def test_hubspot_finds_an_existing_contact_by_phone_instead_of_creating_a_second():
    crm, server = hubspot()
    a = crm.upsert_contact(CUSTOMER)
    b = crm.upsert_contact(CUSTOMER)  # no stored crm id (e.g. lost): found by phone
    assert a == b and len(server.objs["contacts"]) == 1


def test_hubspot_deal_amount_applies_the_discount_and_cancel_closes_it():
    crm, server = hubspot()
    contact = crm.upsert_contact(CUSTOMER)
    did = crm.upsert_deal(contact, BOOKING)
    assert server.objs["deals"][did]["amount"] == "80.10"
    crm.upsert_deal(contact, {**BOOKING, "status": "CANCELLED"})
    assert server.objs["deals"][did]["dealstage"] == "closedlost"


def test_hubspot_dry_run_makes_no_calls_and_placeholder_ids_are_not_reused_as_real_ones():
    crm, server = hubspot(dry_run=True)
    assert crm.upsert_contact(CUSTOMER).startswith("dryrun-") and server.calls == []
    live, server2 = hubspot()
    real = live.upsert_contact({**CUSTOMER, "crm_contact_id": "dryrun-contact-11"})
    assert not real.startswith("dryrun-") and server2.objs["contacts"]


def test_crm_sync_replaces_a_dry_run_placeholder_with_a_real_id(ctx):
    from app import crm_sync

    c = make_customer(ctx, 940)
    ctx.conn.execute("UPDATE customers SET crm_contact_id='dryrun-contact-1' WHERE id=%s", (c.customer_id,))
    crm, _ = hubspot()
    ctx.crm = crm
    crm_sync.enqueue(ctx, c.customer_id, "upsert_contact")
    assert crm_sync.process_outbox(ctx)["done"] >= 1
    assert not ctx.conn.execute("SELECT crm_contact_id FROM customers WHERE id=%s", (c.customer_id,)).fetchone()[0].startswith("dryrun-")


# ------------------------------------------------------------------ Twilio delivery from the outbox

def twilio(handler):
    return TwilioSender("ACtest", "authtoken", "+15125550000", "+14155238886", client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.fixture
def live_ctx(ctx):
    ctx.conn = NoCommit(ctx.conn)  # deliver_outbox commits; the fixture's rollback must still clean up
    return ctx


def queue(ctx, cid, key, channel="sms", body="hello"):
    ctx.messaging.dry_run = False
    ctx.messaging.send(customer_id=cid, channel=channel, body=body, idempotency_key=key)


def test_twilio_sends_only_to_allow_listed_numbers_and_marks_rows(live_ctx):
    ctx = live_ctx
    me = make_customer(ctx, 941)
    other = make_customer(ctx, 942)
    queue(ctx, me.customer_id, "tw-1")
    queue(ctx, other.customer_id, "tw-2")
    seen = []

    def handler(req):
        seen.append((dict(httpx.QueryParams(req.content.decode())), req.headers["authorization"].startswith("Basic ")))
        return httpx.Response(201, json={"sid": "SM1"})

    counts = deliver_outbox(ctx, twilio(handler), {"+15125550941"})
    assert counts["sent"] == 1 and counts["blocked"] >= 1
    assert seen[0][0]["To"] == "+15125550941" and seen[0][0]["Body"] == "hello" and seen[0][1]
    status = lambda k: ctx.conn.execute("SELECT status FROM outbound_messages WHERE idempotency_key=%s", (k,)).fetchone()[0]  # noqa: E731
    assert status("tw-1") == "SENT" and status("tw-2") == "FAILED"


def test_twilio_whatsapp_uses_the_prefix_on_both_numbers(live_ctx):
    ctx = live_ctx
    me = make_customer(ctx, 943)
    queue(ctx, me.customer_id, "tw-wa", channel="whatsapp")
    seen = []
    deliver_outbox(ctx, twilio(lambda r: (seen.append(dict(httpx.QueryParams(r.content.decode()))), httpx.Response(201, json={"sid": "SM2"}))[1]), {"+15125550943"})
    assert seen[0]["To"] == "whatsapp:+15125550943" and seen[0]["From"] == "whatsapp:+14155238886"


def test_twilio_outage_leaves_the_row_queued_and_a_bad_request_marks_it_failed(live_ctx):
    ctx = live_ctx
    me = make_customer(ctx, 944)
    queue(ctx, me.customer_id, "tw-down")
    assert deliver_outbox(ctx, twilio(lambda r: httpx.Response(503)), {"+15125550944"})["deferred"] == 1
    assert ctx.conn.execute("SELECT status FROM outbound_messages WHERE idempotency_key='tw-down'").fetchone()[0] == "QUEUED"
    assert deliver_outbox(ctx, twilio(lambda r: httpx.Response(400)), {"+15125550944"})["failed"] == 1


def test_dry_run_rows_and_telegram_rows_are_never_sent_by_twilio(live_ctx):
    ctx = live_ctx
    me = make_customer(ctx, 945)
    ctx.messaging.dry_run = True
    ctx.messaging.send(customer_id=me.customer_id, channel="sms", body="dry", idempotency_key="tw-dry")
    ctx.messaging.dry_run = False
    ctx.messaging.send(customer_id=None, channel="telegram", body="alert", idempotency_key="tw-tg", recipient="owner")
    assert deliver_outbox(ctx, twilio(lambda r: pytest.fail("must not call Twilio")), {"+15125550945"}) == {"sent": 0, "failed": 0, "blocked": 0, "deferred": 0}


def test_hubspot_uses_the_stored_ids_so_a_lagging_search_index_cannot_create_duplicates():
    """Regression: the first live run made duplicates because the second call searched right after creating."""
    crm, server = hubspot()
    server.search_blind = True
    contact = crm.upsert_contact(CUSTOMER)
    again = crm.upsert_contact({**CUSTOMER, "crm_contact_id": contact, "name": "Maria Q Lopez"})
    deal = crm.upsert_deal(contact, BOOKING)
    deal_again = crm.upsert_deal(contact, {**BOOKING, "crm_deal_id": deal, "status": "CANCELLED"})
    assert contact == again and deal == deal_again
    assert len(server.objs["contacts"]) == 1 and len(server.objs["deals"]) == 1
    assert server.objs["contacts"][contact]["lastname"] == "Q Lopez" and server.objs["deals"][deal]["dealstage"] == "closedlost"


def test_hubspot_falls_back_to_search_then_create_when_the_stored_id_no_longer_exists():
    crm, server = hubspot()
    contact = crm.upsert_contact({**CUSTOMER, "crm_contact_id": "424242"})  # deleted in HubSpot
    assert contact != "424242" and len(server.objs["contacts"]) == 1
    deal = crm.upsert_deal(contact, {**BOOKING, "crm_deal_id": "999"})
    assert deal in server.objs["deals"] and len(server.objs["deals"]) == 1


def test_twilio_whatsapp_template_mode_sends_the_content_sid_and_no_free_text():
    """Trial sandboxes refuse free text over the API (21654); with a template configured we send only the template."""
    seen = []

    def handler(req):
        seen.append(dict(httpx.QueryParams(req.content.decode())))
        return httpx.Response(201, json={"sid": "SM3"})

    sender = TwilioSender("ACtest", "authtoken", "", "+14155550100", client=httpx.Client(transport=httpx.MockTransport(handler)), whatsapp_content_sid="HX123")
    sender.send("whatsapp", "+919999999999", "this text must not be sent")
    assert seen[0]["ContentSid"] == "HX123" and "Body" not in seen[0] and seen[0]["From"] == "whatsapp:+14155550100"
    sender.send("sms", "+15125550000", "sms keeps the body")  # the template only applies to WhatsApp
    assert seen[1]["Body"] == "sms keeps the body" and "ContentSid" not in seen[1]
