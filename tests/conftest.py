import psycopg
import pytest

from app import tracing
from app.adapters.fakes import FakeCalendar, FakeCrm, OutboxMessaging
from app.db.migrate import apply_migrations
from app.settings import get_settings
from app.tools import ToolContext
from tests.helpers import NOW


@pytest.fixture(autouse=True)
def _test_settings(monkeypatch):
    # Tests never depend on the developer's .env secrets, and never send traces to the real
    # Langfuse project (the first run did: fake test calls showed up as "llm-call" traces).
    monkeypatch.setenv("PHONE_HASH_KEY", "test-key")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    tracing.get_client.cache_clear()
    yield
    get_settings.cache_clear()
    tracing.get_client.cache_clear()


@pytest.fixture
def ctx():
    """A tool context on the real Postgres with fakes for calendar/CRM/messaging.

    Everything a test writes is rolled back at the end, so the dev database stays clean."""
    settings = get_settings()
    try:
        conn = psycopg.connect(settings.database_url, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("Postgres is not running (make up)")
    apply_migrations()
    yield ToolContext(
        conn=conn,
        calendar=FakeCalendar(conn),
        crm=FakeCrm(),
        messaging=OutboxMessaging(conn, dry_run=True),
        settings=settings,
        now=lambda: NOW,
    )
    conn.rollback()
    conn.close()
