"""CRM updates go through a queue (crm_outbox) and are retried, so a CRM outage never fails a booking."""

import logging

from psycopg.types.json import Jsonb

from app.adapters.ports import AdapterUnavailable
from app.tools.types import ToolContext

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


def enqueue(ctx: ToolContext, customer_id: int, kind: str, payload: dict | None = None) -> None:
    ctx.conn.execute(
        "INSERT INTO crm_outbox (customer_id, kind, payload) VALUES (%s, %s, %s)",
        (customer_id, kind, Jsonb(payload or {})),
    )


def process_outbox(ctx: ToolContext, limit: int = 20) -> dict[str, int]:
    """Push pending updates to the CRM. Failed ones wait (exponential backoff) and are retried later."""
    done = failed = 0
    rows = ctx.conn.execute(
        "SELECT id, customer_id, kind, payload, attempts FROM crm_outbox"
        " WHERE status = 'PENDING' AND next_attempt_at <= %s ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
        (ctx.now(), limit),
    ).fetchall()
    for row_id, customer_id, kind, payload, attempts in rows:
        try:
            customer = ctx.conn.execute(
                "SELECT id, name, email, address, zip, crm_contact_id, phone FROM customers WHERE id = %s",
                (customer_id,),
            ).fetchone()
            assert customer is not None
            cols = ("id", "name", "email", "address", "zip", "crm_contact_id", "phone")
            c = dict(zip(cols, customer, strict=True))
            contact_id = c["crm_contact_id"] or ctx.crm.upsert_contact(c)
            ctx.conn.execute(
                "UPDATE customers SET crm_contact_id = %s WHERE id = %s", (contact_id, customer_id)
            )
            if kind == "upsert_deal":
                b = ctx.conn.execute(
                    "SELECT id, service_code, start_at, price_usd, discount_pct, status"
                    " FROM bookings WHERE id = %s",
                    (payload["booking_id"],),
                ).fetchone()
                assert b is not None
                deal_id = ctx.crm.upsert_deal(
                    contact_id,
                    dict(zip(("id", "service", "start", "price", "discount", "status"), b, strict=True)),
                )
                ctx.conn.execute("UPDATE bookings SET crm_deal_id = %s WHERE id = %s", (deal_id, b[0]))
            ctx.conn.execute("UPDATE crm_outbox SET status = 'DONE' WHERE id = %s", (row_id,))
            done += 1
        except AdapterUnavailable as exc:
            attempts += 1
            status = "FAILED" if attempts >= MAX_ATTEMPTS else "PENDING"
            wait_s = min(3600, 30 * 2**attempts)
            ctx.conn.execute(
                "UPDATE crm_outbox SET attempts = %s, status = %s, last_error = %s,"
                " next_attempt_at = %s + make_interval(secs => %s) WHERE id = %s",
                (attempts, status, str(exc)[:200], ctx.now(), wait_s, row_id),
            )
            failed += 1
    return {"done": done, "failed": failed}
