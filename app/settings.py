"""All configuration comes from environment variables (or .env). Nothing secret lives in code."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://p03_app@127.0.0.1:5435/p03"
    migrator_database_url: str = "postgresql://p03_migrator@127.0.0.1:5435/p03"

    # Safety first: unless turned off, nothing leaves the machine (CLAUDE.md safety rules).
    dry_run: bool = True
    # HMAC key for phone numbers. A plain hash of a phone number is reversible by brute force
    # (only ~10 billion US numbers), a keyed hash is not without the key.
    phone_hash_key: SecretStr = SecretStr("")

    log_level: str = "INFO"

    # --- LLM (everything goes through app/llm.py) ---
    anthropic_api_key: SecretStr = SecretStr("")
    # Cheap tier by default. Verified 2026-10-09: $0.10 in / $0.50 out per 1M tokens.
    llm_default_model: str = "claude-haiku-5-5"
    # Used when the default model keeps failing (provider incident): a different, stronger model.
    llm_fallback_model: str = "claude-sonnet-5-5"
    llm_timeout_s: float = 30.0
    llm_max_retries: int = 3
    # Cost guard (CLAUDE.md spend rules): ask the owner above this many dollars.
    cost_confirm_above_usd: float = 0.50

    # --- Langfuse tracing; with no keys every tracing call quietly does nothing ---
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_host: str = "https://cloud.langfuse.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
