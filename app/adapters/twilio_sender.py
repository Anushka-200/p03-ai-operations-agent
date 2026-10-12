"""Delivers queued SMS/WhatsApp rows from the outbox through Twilio's REST API.

The outbox pattern: the agent and tools only WRITE a row to `outbound_messages`. This separate step reads
QUEUED rows and sends them. That is why dry-run is trivial (rows are DRY_RUN and never read here), why a
Twilio outage cannot break a conversation (rows wait and are retried), and why "the agent can only reply to
the verified sender" is easy to prove (the recipient is resolved here from the database, never from the model).

Two extra guards for the live demo: only numbers in `allowed_to` (your own verified phone) may be texted,
and a row that fails is marked FAILED with a short reason, not retried forever.
At-least-once: a crash between Twilio accepting a message and our UPDATE could repeat one text.
"""

import logging
from collections.abc import Collection

import httpx

from app.adapters.http import AdapterRejected, call
from app.adapters.ports import AdapterUnavailable
from app.adapters.resilience import CircuitBreaker
from app.tools.types import ToolContext

logger = logging.getLogger(__name__)
API = "https://api.twilio.com/2010-04-01/Accounts"


class TwilioSender:
    def __init__(
        self, account_sid: str, auth_token: str, sms_from: str, whatsapp_from: str = "",
        client: httpx.Client | None = None, breaker: CircuitBreaker | None = None, whatsapp_content_sid: str = "",
    ) -> None:  # fmt: skip
        self.sid, self._token, self.sms_from, self.whatsapp_from = (
            account_sid,
            auth_token,
            sms_from,
            whatsapp_from,
        )
        # A trial/sandbox account may refuse free text over the API (error 21654) and accept only an approved
        # template. When set, WhatsApp sends use the template (its fixed text; our body is NOT sent).
        self.whatsapp_content_sid = whatsapp_content_sid
        self.client = client or httpx.Client()
        self.breaker = breaker or CircuitBreaker("twilio")

    def send(self, channel: str, to: str, body: str) -> str:
        """Returns Twilio's MessageSid. WhatsApp numbers carry a 'whatsapp:' prefix on both ends."""
        payload = {"Body": body}
        if channel == "whatsapp":
            frm, to = f"whatsapp:{self.whatsapp_from}", f"whatsapp:{to}"
            if self.whatsapp_content_sid:
                payload = {"ContentSid": self.whatsapp_content_sid}
        else:
            frm = self.sms_from
        resp = self.breaker.call(
            lambda: call(
                self.client, "POST", f"{API}/{self.sid}/Messages.json", name="twilio", ok=(200, 201),
                auth=(self.sid, self._token), data={"From": frm, "To": to, **payload},
            )
        )  # fmt: skip
        return str(resp.json()["sid"])


def deliver_outbox(
    ctx: ToolContext, sender: TwilioSender, allowed_to: Collection[str], limit: int = 20
) -> dict[str, int]:
    """Send QUEUED customer/technician messages. Telegram rows belong to the bot, not to Twilio."""
    counts = {"sent": 0, "failed": 0, "blocked": 0, "deferred": 0}
    rows = ctx.conn.execute(
        "SELECT m.id, m.channel, m.body, COALESCE(m.recipient, c.phone) FROM outbound_messages m"
        " LEFT JOIN customers c ON c.id = m.customer_id"
        " WHERE m.status = 'QUEUED' AND m.channel IN ('sms', 'whatsapp') ORDER BY m.id LIMIT %s"
        " FOR UPDATE OF m SKIP LOCKED",
        (limit,),
    ).fetchall()
    for mid, channel, body, to in rows:
        if not to or to not in allowed_to:
            # Not on the allow-list: leave it QUEUED-but-blocked as FAILED so nothing sends by accident.
            ctx.conn.execute("UPDATE outbound_messages SET status='FAILED' WHERE id=%s", (mid,))
            counts["blocked"] += 1
            continue
        try:
            sender.send(channel, to, body)
        except AdapterUnavailable:
            counts["deferred"] += 1  # stays QUEUED: tried again next round
            continue
        except AdapterRejected:
            ctx.conn.execute("UPDATE outbound_messages SET status='FAILED' WHERE id=%s", (mid,))
            counts["failed"] += 1
            continue
        ctx.conn.execute("UPDATE outbound_messages SET status='SENT', sent_at=now() WHERE id=%s", (mid,))
        counts["sent"] += 1
    ctx.conn.commit()
    logger.info("twilio delivery", extra=counts)
    return counts
