"""Prepare the Telegram live run (task 3.5): Maria talks to the REAL model until her discount request is
PENDING (the thread is left paused on purpose, in the dev database). Then start the bot and tap Approve on your phone.
`uv run python -m scripts.live_telegram_prep cleanup` removes every trace afterwards.

Original docstring: Live run (task 2.10): a scripted customer "Maria" talks to the REAL model through the real webhook,
graph, gate and Postgres checkpoints. DRY_RUN stays on: nothing is sent anywhere, the calendar is fake.

Usage: uv run python -m scripts.live_maria        (projected cost is printed first; ~ $0.01-0.03)
"""

import os
from urllib.parse import urlencode

import psycopg
from fastapi.testclient import TestClient
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

from app import llm
from app.agent.graph import build_graph
from app.agent.run import Agent, setup_checkpointer
from app.api.main import create_app
from app.api.twilio_signature import compute_signature
from app.settings import get_settings

PHONE = "+15125559100"
MESSAGES = [
    "Hi, my AC stopped cooling",
    "I'm Maria Lopez, 1200 Oak St, 78704",
    "How much is the diagnostic visit? Can you come Monday morning?",
    "the first one please",
    "yes, please book it",
    "Can I get 10% off? I am a returning customer",
]

os.environ.setdefault("TWILIO_AUTH_TOKEN", "live-run-token")  # signs our own fake webhook requests
get_settings.cache_clear()
s = get_settings()
assert s.dry_run, "refusing to run with DRY_RUN=false"
print(f"projected cost: ~$0.02 (6 messages, Haiku); guard threshold ${s.cost_confirm_above_usd:.2f}")

records: list[llm.LLMResult] = []


def recording_llm(messages, **kw):
    res = llm.complete(messages, **kw)
    records.append(res)
    return res


def cleanup():
    with psycopg.connect(s.migrator_database_url, autocommit=True) as o:
        row = o.execute("SELECT id, phone_hash FROM customers WHERE phone=%s", (PHONE,)).fetchone()
        if not row:
            return
        cid, h = row
        for t in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            o.execute(f"DELETE FROM {t} WHERE thread_id=%s", (h,))
        inb = [r[0] for r in o.execute("SELECT id FROM inbound_messages WHERE customer_id=%s", (cid,))]
        o.execute(
            "DELETE FROM outbound_messages WHERE customer_id=%s OR idempotency_key LIKE ANY(%s)",
            (cid, [f"emerg:{i}:%" for i in inb] + [f"handoff:{cid}:%"]),
        )
        o.execute(
            "DELETE FROM calendar_events WHERE booking_id IN (SELECT id FROM bookings WHERE customer_id=%s)",
            (cid,),
        )
        for t in (
            "crm_outbox",
            "bookings",
            "approvals",
            "followups",
            "emergencies",
            "inbound_messages",
            "audit_log",
        ):
            o.execute(f"DELETE FROM {t} WHERE customer_id=%s", (cid,))
        o.execute("DELETE FROM idempotency_keys WHERE key LIKE %s", (f"book:{cid}:%",))
        o.execute("DELETE FROM customers WHERE id=%s", (cid,))


if len(__import__("sys").argv) > 1 and __import__("sys").argv[1] == "cleanup":
    cleanup()
    print("cleaned")
    raise SystemExit

cleanup()
setup_checkpointer()
conn = psycopg.connect(s.database_url, autocommit=True, row_factory=dict_row)
agent = Agent(build_graph(PostgresSaver(conn)), recording_llm)
client = TestClient(create_app(agent_fn=agent.run_turn))
URL = s.twilio_webhook_url

try:
    for i, text in enumerate(MESSAGES, 1):
        params = {"MessageSid": f"SM-live-{i}", "From": PHONE, "Body": text}
        sig = compute_signature(URL, params, s.twilio_auth_token.get_secret_value())
        r = client.post(
            "/webhooks/twilio",
            content=urlencode(params),
            headers={"X-Twilio-Signature": sig, "Content-Type": "application/x-www-form-urlencoded"},
        )
        with psycopg.connect(s.migrator_database_url) as c:
            reply = c.execute(
                "SELECT body, status FROM outbound_messages WHERE idempotency_key=%s",
                (
                    f"in:{c.execute('SELECT id FROM inbound_messages WHERE message_sid=%s', (f'SM-live-{i}',)).fetchone()[0]}:reply",
                ),
            ).fetchone()
        print(
            f"\n[{i}] Maria : {text}\n    webhook {r.status_code}\n    Agent : {reply[0] if reply else '(no reply)'}  [{reply[1] if reply else ''}]"
        )

    with psycopg.connect(s.migrator_database_url) as c:
        cid = c.execute("SELECT id FROM customers WHERE phone=%s", (PHONE,)).fetchone()[0]
        print("\n--- tools called (audit_log) ---")
        for tool, dec, reason in c.execute(
            "SELECT tool, decision, reason FROM audit_log WHERE customer_id=%s ORDER BY id", (cid,)
        ):
            print(f"  {tool:24} {dec:15} {reason}")
        print(
            "bookings:",
            c.execute("SELECT status, discount_pct FROM bookings WHERE customer_id=%s", (cid,)).fetchall(),
        )
        print(
            "approvals:",
            c.execute("SELECT kind, status FROM approvals WHERE customer_id=%s", (cid,)).fetchall(),
        )
        print(
            "outbound statuses:",
            c.execute(
                "SELECT status, count(*) FROM outbound_messages WHERE customer_id=%s GROUP BY 1", (cid,)
            ).fetchall(),
        )
    cost = sum(r.cost_usd or 0 for r in records)
    print(
        f"\nLLM calls: {len(records)} | input tokens: {sum(r.input_tokens for r in records)} | output tokens: {sum(r.output_tokens for r in records)} | cost: ${cost:.4f} | models: {sorted({r.model for r in records})}"
    )
finally:
    conn.close()  # NOT cleaned up: the paused approval is what the bot will show you
