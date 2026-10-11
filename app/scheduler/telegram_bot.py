"""The Telegram bot process: long-poll for owner messages and button taps, and push approvals/alerts.

Run with `uv run python -m app.scheduler.telegram_bot`. It makes no inbound connection and needs no
public URL. Without TELEGRAM_LIVE=true it only reads and logs what it would send (per-integration
dry-run). A separate process from the follow-up scheduler so a slow model call cannot delay follow-ups.
"""

import logging
import time

import psycopg

from app import owner_bot
from app.adapters.fakes import FakeCalendar, FakeCrm, OutboxMessaging
from app.adapters.telegram import TelegramApi, TelegramError, TelegramPort
from app.log import setup_logging
from app.settings import Settings, get_settings
from app.tools.types import ToolContext

logger = logging.getLogger(__name__)
MAX_BACKOFF_S = 60


def make_ctx(conn: psycopg.Connection, s: Settings) -> ToolContext:
    return ToolContext(
        conn=conn, calendar=FakeCalendar(conn), crm=FakeCrm(),
        messaging=OutboxMessaging(conn, dry_run=s.dry_run), settings=s,
    )  # fmt: skip


def poll_once(s: Settings, tg: TelegramPort, agent, offset: int, history: list[dict[str, str]], llm=None) -> int:
    """One round: handle new updates, then push approvals/alerts. Returns the next offset."""
    kw = {"llm": llm} if llm else {}
    for update in tg.get_updates(offset, s.telegram_poll_s):
        try:
            with psycopg.connect(s.database_url) as conn:  # one transaction per update
                label = owner_bot.handle_update(make_ctx(conn, s), tg, agent, update, history, **kw)
            logger.info("telegram update handled", extra={"route": label})
        except TelegramError:
            raise
        except Exception:
            logger.exception("telegram update failed; skipping it")  # never loop forever on one bad update
        offset = update["update_id"] + 1  # tells Telegram this one is done
    with psycopg.connect(s.database_url) as conn:
        ctx = make_ctx(conn, s)
        owner_bot.notify_pending_approvals(ctx, tg, s.telegram_owner_id)
        owner_bot.deliver_owner_alerts(ctx, tg, s.telegram_owner_id)
    return offset


def main() -> None:
    setup_logging()
    s = get_settings()
    if not s.telegram_bot_token.get_secret_value() or s.telegram_owner_id == 0:
        logger.error("TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID must be set in .env (task 3.2)")
        raise SystemExit(2)
    from app.agent.run import default_agent

    tg = TelegramApi(s.telegram_bot_token.get_secret_value(), send_enabled=s.telegram_live)
    agent = default_agent()
    logger.info("telegram bot started", extra={"live": s.telegram_live})
    offset, failures = 0, 0
    history: list[dict[str, str]] = []
    while True:
        try:
            offset = poll_once(s, tg, agent, offset, history)
            failures = 0
        except (TelegramError, psycopg.Error) as exc:
            failures += 1
            wait = min(MAX_BACKOFF_S, 2**failures)  # back off instead of hammering Telegram or the DB
            logger.warning("poll failed (%s); retry in %ss", type(exc).__name__, wait)
            time.sleep(wait)


if __name__ == "__main__":
    main()
