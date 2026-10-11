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

    # --- Agent caps (PLAN: hitting any cap = hand off to a person + alert the owner) ---
    agent_max_tool_calls_per_message: int = 8
    agent_max_tool_calls_per_conversation: int = 40
    agent_max_cost_per_conversation_usd: float = 0.25
    agent_max_output_tokens: int = 1024

    # --- Twilio webhook. Twilio signs the exact public URL it called, so we need it here; with a
    # fake signed request in tests it is just a string. Empty token = every request is rejected. ---
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_webhook_url: str = "http://localhost:8003/webhooks/twilio"
    emergency_classifier: bool = False  # extra LLM check for paraphrases; rules always run first

    # --- Langfuse tracing; with no keys every tracing call quietly does nothing ---
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_host: str = "https://cloud.langfuse.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
