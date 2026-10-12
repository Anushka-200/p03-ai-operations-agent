"""The scheduler process: once a minute, run the follow-up tick. Runs separately from the API
(`docker compose` service `scheduler` in Phase 6; the Telegram bot joins it in Phase 3.3)."""

import logging

import psycopg
from apscheduler.schedulers.blocking import BlockingScheduler

from app import crm_sync, followups
from app.adapters.factory import make_context
from app.adapters.twilio_sender import TwilioSender, deliver_outbox
from app.log import setup_logging
from app.settings import get_settings

logger = logging.getLogger(__name__)


def run_tick() -> None:
    s = get_settings()
    try:
        with psycopg.connect(s.database_url) as conn:  # commits on clean exit, rolls back on error
            ctx = make_context(conn, s)
            followups.tick(ctx)
    except Exception:
        logger.exception("followup tick failed; will retry next minute")


def run_delivery() -> None:
    """Send QUEUED SMS/WhatsApp rows through Twilio. Does nothing unless TWILIO_LIVE=true AND DRY_RUN=false."""
    s = get_settings()
    if not s.twilio_live or s.dry_run:
        return
    allowed = {n.strip() for n in s.twilio_allowed_to.split(",") if n.strip()}
    sender = TwilioSender(s.twilio_account_sid, s.twilio_auth_token.get_secret_value(), s.twilio_sms_from, s.twilio_whatsapp_from)
    try:
        with psycopg.connect(s.database_url) as conn:
            deliver_outbox(make_context(conn, s), sender, allowed)
    except Exception:
        logger.exception("delivery failed; will retry")


def run_crm_sync() -> None:
    """Push queued contact/deal updates to the CRM; failures wait with backoff (crm_outbox)."""
    s = get_settings()
    try:
        with psycopg.connect(s.database_url) as conn:
            crm_sync.process_outbox(make_context(conn, s))
    except Exception:
        logger.exception("crm sync failed; will retry")


def build_scheduler() -> BlockingScheduler:
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(run_tick, "interval", seconds=60, max_instances=1, coalesce=True, id="followup_tick")
    sched.add_job(run_delivery, "interval", seconds=15, max_instances=1, coalesce=True, id="twilio_delivery")
    sched.add_job(run_crm_sync, "interval", seconds=30, max_instances=1, coalesce=True, id="crm_sync")
    return sched


def main() -> None:
    setup_logging()
    sched = build_scheduler()
    logger.info("scheduler started")
    sched.start()


if __name__ == "__main__":
    main()
