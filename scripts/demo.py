"""The demo (`make demo`): the whole system, for real, in about two minutes. DRY_RUN stays on.

What you will see, in order:
  1. A real uvicorn server starts. "Maria" texts it through SIGNED webhooks (the same bytes Twilio would send).
  2. Halfway through, the server is KILLED and started again. Maria carries on: her conversation lives in
     Postgres checkpoints, not in the server's memory.
  3. She books a visit, then asks for a discount. The request is held for the owner (approval queue).
  4. The "owner" approves (same function the Telegram button calls); the paused conversation resumes and
     Maria is told the outcome.
  5. A second customer writes "I smell gas": a fixed rule escalates it. No language model is involved.
  6. We print the outbox: every message the system WOULD have sent. Nothing left the machine.

Cost: about $0.01 on Haiku. Usage: uv run python -m scripts.demo   (--keep leaves the demo data in the database)
"""

import os
import subprocess
import sys
import time
from urllib.parse import urlencode

import httpx
import psycopg

PORT = 8013  # not 8003, so the demo never collides with a compose-run API
PHONE, GAS_PHONE = "+15125559101", "+15125559102"
os.environ.update(
    TWILIO_WEBHOOK_URL=f"http://localhost:{PORT}/webhooks/twilio",
    TWILIO_AUTH_TOKEN="demo-token",
    DRY_RUN="true",
)

from app.api.twilio_signature import compute_signature  # noqa: E402
from app.settings import get_settings  # noqa: E402

s = get_settings()
assert s.dry_run, "refusing to run with DRY_RUN=false"
URL = s.twilio_webhook_url
BASE = f"http://localhost:{PORT}"
MARIA = [
    "Hi, my AC stopped cooling",
    "I'm Maria Lopez, 1200 Oak St, 78704",
    "How much is the diagnostic visit? Can you come tomorrow morning?",
    "RESTART",
    "the first one please",
    "yes, please book it",
    "Can I get 10% off? I'm a returning customer",
]


def say(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def cleanup() -> None:
    with psycopg.connect(s.migrator_database_url, autocommit=True) as o:
        for phone in (PHONE, GAS_PHONE):
            row = o.execute("SELECT id, phone_hash FROM customers WHERE phone=%s", (phone,)).fetchone()
            if not row:
                continue
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
                "inbound_messages",
                "emergencies",
                "audit_log",
            ):
                o.execute(f"DELETE FROM {t} WHERE customer_id=%s", (cid,))
            o.execute("DELETE FROM idempotency_keys WHERE key LIKE %s", (f"%:{cid}:%",))
            o.execute("DELETE FROM customers WHERE id=%s", (cid,))


def start_server() -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.api.main:create_app",
            "--factory",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        env=os.environ.copy(),
    )
    for _ in range(60):
        try:
            if httpx.get(f"{BASE}/health", timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.5)
    proc.kill()
    raise SystemExit("server did not start")


def send(sid: str, phone: str, text: str) -> str:
    params = {"MessageSid": sid, "From": phone, "Body": text}
    sig = compute_signature(URL, params, "demo-token")
    r = httpx.post(f"{BASE}/webhooks/twilio", content=urlencode(params), timeout=10,
                   headers={"X-Twilio-Signature": sig, "Content-Type": "application/x-www-form-urlencoded"})  # fmt: skip
    assert r.status_code == 200, r.text
    with psycopg.connect(s.migrator_database_url, autocommit=True) as c:
        for _ in range(120):  # the reply is produced in the background; wait for it (max ~60 s)
            row = c.execute(
                "SELECT id, processed_at FROM inbound_messages WHERE message_sid=%s", (sid,)
            ).fetchone()
            if row and row[1]:
                rep = c.execute(
                    "SELECT body FROM outbound_messages WHERE idempotency_key LIKE %s ORDER BY id LIMIT 1",
                    (f"in:{row[0]}:%",),
                ).fetchone()
                return rep[0] if rep else "(no reply)"
            time.sleep(0.5)
    return "(timed out)"


def main() -> None:
    keep = "--keep" in sys.argv
    cleanup()
    server = start_server()
    try:
        say("1. Maria texts the agent (signed webhooks, real model, dry-run)")
        for i, text in enumerate(MARIA, 1):
            if text == "RESTART":
                say("2. SERVER KILLED AND RESTARTED: conversation memory lives in Postgres")
                server.terminate()
                server.wait(10)
                server = start_server()
                continue
            print(f"\nMaria : {text}")
            print(f"Agent : {send(f'SM-demo-{i}', PHONE, text)}", flush=True)

        with psycopg.connect(s.migrator_database_url, autocommit=True) as c:
            cid = c.execute("SELECT id FROM customers WHERE phone=%s", (PHONE,)).fetchone()[0]
            appr = c.execute(
                "SELECT id, kind, status FROM approvals WHERE customer_id=%s ORDER BY id DESC LIMIT 1", (cid,)
            ).fetchone()
        say("3. The discount is held for the owner")
        print("approval queue:", appr)

        say("4. The owner approves (the same function the Telegram button calls)")
        if appr and appr[2] == "PENDING":
            from app.adapters.factory import make_context
            from app.agent.run import default_agent
            from app.owner_bot import handle_decision

            with psycopg.connect(s.database_url) as conn:
                ctx = make_context(conn, s)
                print("owner sees:", handle_decision(ctx, default_agent(), appr[0], True))

        say("5. A second customer writes about gas: fixed rule, no model")
        t0 = time.perf_counter()
        print("Customer : I smell gas in my kitchen")
        print(f"System   : {send('SM-demo-gas', GAS_PHONE, 'I smell gas in my kitchen')}")
        print(f"(answered in {time.perf_counter() - t0:.1f} s, 0 model calls)")

        say("6. The outbox: every message the system would send (nothing was sent)")
        with psycopg.connect(s.migrator_database_url, autocommit=True) as c:
            for who, chan, status, body in c.execute(
                "SELECT COALESCE(c.name, 'staff'), o.channel, o.status, o.body FROM outbound_messages o"
                " LEFT JOIN customers c ON c.id = o.customer_id"
                " WHERE o.customer_id IN (SELECT id FROM customers WHERE phone IN (%s, %s)) OR o.idempotency_key LIKE 'emerg:%%'"
                " ORDER BY o.id DESC LIMIT 14", (PHONE, GAS_PHONE),
            ):  # fmt: skip
                print(f"  [{status:7}] {chan:8} -> {who:12} {body[:110]!r}")
            print(
                "bookings:",
                c.execute(
                    "SELECT status, discount_pct, price_usd FROM bookings WHERE customer_id=%s", (cid,)
                ).fetchall(),
            )
            print(
                "audit rows for Maria:",
                c.execute("SELECT count(*) FROM audit_log WHERE customer_id=%s", (cid,)).fetchone()[0],
            )
    finally:
        server.terminate()
        if not keep:
            cleanup()
            print("\n(demo data removed; use --keep to leave it in the database)")


if __name__ == "__main__":
    main()
