"""The scheduler process: once a minute, run the follow-up tick. Runs separately from the API
(`docker compose` service `scheduler` in Phase 6; the Telegram bot joins it in Phase 3.3)."""

import logging

import psycopg
from apscheduler.schedulers.blocking import BlockingScheduler

from app import followups
from app.adapters.fakes import FakeCalendar, FakeCrm, OutboxMessaging
from app.log import setup_logging
from app.settings import get_settings
from app.tools.types import ToolContext

logger = logging.getLogger(__name__)


def run_tick() -> None:
    s = get_settings()
    try:
        with psycopg.connect(s.database_url) as conn:  # commits on clean exit, rolls back on error
            ctx = ToolContext(
                conn=conn, calendar=FakeCalendar(conn), crm=FakeCrm(),
                messaging=OutboxMessaging(conn, dry_run=s.dry_run), settings=s,
            )  # fmt: skip
            followups.tick(ctx)
    except Exception:
        logger.exception("followup tick failed; will retry next minute")


def main() -> None:
    setup_logging()
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(run_tick, "interval", seconds=60, max_instances=1, coalesce=True, id="followup_tick")
    logger.info("scheduler started")
    sched.start()


if __name__ == "__main__":
    main()
