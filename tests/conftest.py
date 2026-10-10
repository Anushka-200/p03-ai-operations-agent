import pytest

from app import tracing
from app.settings import get_settings


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
