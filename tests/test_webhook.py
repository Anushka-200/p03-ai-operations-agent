"""Twilio webhook through the real FastAPI app and real Postgres, with signed fake requests (no tunnel)."""

from urllib.parse import urlencode

import psycopg
import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app.api.main import create_app
from app.api.twilio_signature import compute_signature
from app.settings import get_settings

TOKEN = "test-twilio-token"
URL = "http://localhost:8003/webhooks/twilio"
PHONE_A, PHONE_B = "+15125559001", "+15125559002"


@pytest.fixture(autouse=True)
def _twilio_env(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    get_settings.cache_clear()


def _owner():
    return psycopg.connect(get_settings().migrator_database_url, autocommit=True, connect_timeout=2)


def _wipe(owner):
    ids = [r[0] for r in owner.execute("SELECT id FROM customers WHERE phone LIKE '+151255590%'").fetchall()]
    if not ids:
        return
    for (h,) in owner.execute("SELECT phone_hash FROM customers WHERE id = ANY(%s)", (ids,)).fetchall():
        for t in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            owner.execute(f"DELETE FROM {t} WHERE thread_id = %s", (h,))
    inb = [
        r[0]
        for r in owner.execute(
            "SELECT id FROM inbound_messages WHERE customer_id = ANY(%s)", (ids,)
        ).fetchall()
    ]
    owner.execute(
        "DELETE FROM outbound_messages WHERE customer_id = ANY(%s) OR idempotency_key LIKE ANY(%s)",
        (ids, [f"emerg:{i}:%" for i in inb]),
    )
    owner.execute("DELETE FROM emergencies WHERE customer_id = ANY(%s)", (ids,))
    owner.execute("DELETE FROM followups WHERE customer_id = ANY(%s)", (ids,))
    owner.execute("DELETE FROM inbound_messages WHERE customer_id = ANY(%s)", (ids,))
    owner.execute("DELETE FROM audit_log WHERE customer_id = ANY(%s)", (ids,))
    owner.execute("DELETE FROM customers WHERE id = ANY(%s)", (ids,))


@pytest.fixture
def db():
    try:
        owner = _owner()
    except psycopg.OperationalError:
        pytest.skip("Postgres is not running (make up)")
    from app.db.migrate import apply_migrations

    apply_migrations()
    _wipe(owner)
    yield owner
    _wipe(owner)
    owner.close()


def post(client, sid, body, sender=PHONE_A, sign=True, signature=None):
    params = {"MessageSid": sid, "From": sender, "Body": body}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if sign:
        headers["X-Twilio-Signature"] = signature or compute_signature(URL, params, TOKEN)
    return client.post("/webhooks/twilio", content=urlencode(params), headers=headers)


class Agent:
    """Stub agent: records calls so tests can prove it was (not) used."""

    def __init__(self, reply="Hi, this is CoolBreeze's virtual assistant."):
        self.calls, self.reply = [], reply

    def __call__(self, ctx, caller, inbound_id, text):
        self.calls.append((caller, text))
        return self.reply


def rows(owner, sql, *args):
    return owner.execute(sql, args).fetchall()


def test_signature_matches_twilios_own_validator():
    params = {"MessageSid": "SM1", "From": "+15125559001", "Body": "hello there"}
    ours = compute_signature(URL, params, TOKEN)
    assert RequestValidator(TOKEN).validate(URL, params, ours)


def test_bad_or_missing_signature_is_rejected_and_nothing_is_saved(db):
    agent = Agent()
    client = TestClient(create_app(agent_fn=agent))
    assert post(client, "SM-bad-1", "hi", signature="AAAA").status_code == 403
    assert post(client, "SM-bad-2", "hi", sign=False).status_code == 403
    assert rows(db, "SELECT 1 FROM inbound_messages WHERE message_sid LIKE 'SM-bad-%%'") == []
    assert agent.calls == []


def test_valid_message_is_saved_then_processed_and_replied_in_dry_run(db):
    agent = Agent()
    client = TestClient(create_app(agent_fn=agent))
    res = post(client, "SM-ok-1", "I'd like an AC tune-up")
    assert res.status_code == 200 and "<Response" in res.text
    assert len(agent.calls) == 1
    assert agent.calls[0][0].role == "customer"  # customer id comes from the verified From, not the text
    sid, processed = rows(
        db, "SELECT message_sid, processed_at FROM inbound_messages WHERE message_sid='SM-ok-1'"
    )[0]
    assert processed is not None
    out = rows(
        db,
        "SELECT status, channel FROM outbound_messages WHERE idempotency_key LIKE 'in:%%:reply' AND body LIKE 'Hi, this is%%' ORDER BY id DESC LIMIT 1",
    )
    assert out == [("DRY_RUN", "sms")]


def test_duplicate_delivery_is_processed_once(db):
    agent = Agent()
    client = TestClient(create_app(agent_fn=agent))
    assert post(client, "SM-dup-1", "book me please").status_code == 200
    assert post(client, "SM-dup-1", "book me please").status_code == 200  # Twilio retried: still 200
    assert len(agent.calls) == 1
    assert rows(db, "SELECT count(*) FROM inbound_messages WHERE message_sid='SM-dup-1'") == [(1,)]


def test_emergency_never_reaches_the_agent_and_pages_once(db):
    agent = Agent()
    client = TestClient(create_app(agent_fn=agent))
    assert post(client, "SM-em-1", "I smell gas in the kitchen").status_code == 200
    assert post(client, "SM-em-1", "I smell gas in the kitchen").status_code == 200  # duplicate delivery
    assert agent.calls == []
    assert rows(
        db,
        "SELECT count(*) FROM emergencies e JOIN customers c ON c.id=e.customer_id WHERE c.phone=%s",
        PHONE_A,
    ) == [(1,)]
    inbound_id = rows(db, "SELECT id FROM inbound_messages WHERE message_sid='SM-em-1'")[0][0]
    sent = rows(
        db,
        "SELECT recipient FROM outbound_messages WHERE idempotency_key LIKE %s ORDER BY id",
        f"emerg:{inbound_id}:%",
    )
    assert len(sent) == 3 and ("owner",) in sent
    assert rows(db, "SELECT lead_status FROM customers WHERE phone=%s", PHONE_A) == [("EMERGENCY",)]
    # a follow-up message while paused still does not reach the agent
    assert post(client, "SM-em-2", "when will someone call?").status_code == 200
    assert agent.calls == []


def test_stop_opts_out_cancels_followups_and_silences_the_agent(db):
    agent = Agent()
    client = TestClient(create_app(agent_fn=agent))
    post(client, "SM-st-0", "hello", sender=PHONE_B)
    cid = rows(db, "SELECT id FROM customers WHERE phone=%s", PHONE_B)[0][0]
    # the pipeline itself scheduled the Day 0/1/3/7 series after the first message
    assert rows(db, "SELECT count(*) FROM followups WHERE customer_id=%s AND status='PENDING'", cid) == [(4,)]
    agent.calls.clear()
    post(client, "SM-st-1", "STOP", sender=PHONE_B)
    assert rows(db, "SELECT opted_out_at IS NOT NULL FROM customers WHERE id=%s", cid) == [(True,)]
    assert {r[0] for r in rows(db, "SELECT status FROM followups WHERE customer_id=%s", cid)} == {"CANCELLED"}
    post(client, "SM-st-2", "hello again", sender=PHONE_B)
    assert agent.calls == []  # we do not answer someone who opted out
    post(client, "SM-st-3", "START", sender=PHONE_B)
    assert rows(db, "SELECT opted_out_at FROM customers WHERE id=%s", cid) == [(None,)]


def test_whatsapp_prefix_sets_the_channel(db):
    client = TestClient(create_app(agent_fn=Agent()))
    post(client, "SM-wa-1", "hi", sender="whatsapp:+15125559002")
    assert rows(db, "SELECT channel FROM inbound_messages WHERE message_sid='SM-wa-1'") == [("whatsapp",)]


def test_full_path_webhook_to_graph_to_reply_with_a_restart_between_messages(db):
    """Signed webhook -> save -> rules -> REAL graph + Postgres checkpoints (scripted model) -> dry-run reply."""
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    from app.agent.graph import build_graph
    from app.agent.run import Agent, setup_checkpointer
    from tests.test_agent import FakeLLM, say

    setup_checkpointer()

    def new_process(llm):  # a new graph + new checkpointer connection = a restarted server
        conn = psycopg.connect(get_settings().database_url, autocommit=True, row_factory=dict_row)
        agent = Agent(build_graph(PostgresSaver(conn)), llm)
        return TestClient(create_app(agent_fn=agent.run_turn)), conn

    llm1 = FakeLLM(say("Happy to help. What's the problem with your AC?"))
    client, conn1 = new_process(llm1)
    post(client, "SM-full-1", "Hi, need help with my AC")
    conn1.close()

    llm2 = FakeLLM(say("Thanks. A tune-up visit works. What's your ZIP?"))
    client, conn2 = new_process(llm2)  # restart between the two messages
    post(client, "SM-full-2", "it is blowing warm air")
    conn2.close()

    assert any(
        "Hi, need help" in str(m.get("content")) for m in llm2.calls[0][0]
    )  # remembered across the restart
    replies = rows(
        db,
        "SELECT body FROM outbound_messages WHERE idempotency_key LIKE 'in:%%:reply' AND customer_id=(SELECT id FROM customers WHERE phone=%s) ORDER BY id",
        PHONE_A,
    )
    assert len(replies) == 2 and replies[0][0].startswith("Hi, I'm CoolBreeze's virtual assistant.")
    assert not replies[1][0].startswith("Hi, I'm")


def test_handoff_status_keeps_the_agent_out_but_sends_a_holding_text(db):
    agent = Agent()
    client = TestClient(create_app(agent_fn=agent))
    post(client, "SM-ho-0", "hello", sender=PHONE_B)
    db.execute("UPDATE customers SET lead_status='HANDOFF' WHERE phone=%s", (PHONE_B,))
    agent.calls.clear()
    post(client, "SM-ho-1", "are you there?", sender=PHONE_B)
    assert agent.calls == []
    assert (
        rows(db, "SELECT count(*) FROM outbound_messages WHERE idempotency_key LIKE 'handoff-hold:%%'")[0][0]
        >= 1
    )
