"""All configuration comes from environment variables (or .env). Nothing secret lives in code."""

from functools import lru_cache
from typing import Literal

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
    llm_prompt_caching: bool = False  # experiment E2: cache the system prompt + tool schemas
    llm_max_retries: int = 3
    # Cost guard (CLAUDE.md spend rules): ask the owner above this many dollars.
    cost_confirm_above_usd: float = 0.50

    # --- Agent caps (PLAN: hitting any cap = hand off to a person + alert the owner) ---
    agent_max_tool_calls_per_message: int = 8
    agent_max_tool_calls_per_conversation: int = 40
    agent_max_cost_per_conversation_usd: float = 0.25
    agent_max_output_tokens: int = 1024
    # Compaction (context engineering): above this many (estimated) history tokens, older turns are
    # summarised and only the last few customer turns stay verbatim. Switchable for experiment E4.
    agent_mode: Literal["single", "supervisor"] = "single"  # single | supervisor (ADR 006): one agent with all tools, or a router + 2 specialists
    agent_tool_mode: Literal["classic", "lean"] = "classic"  # lean (E3): price table in the prompt, ZIP check merged into update_my_contact
    llm_agent_model: str = ""  # E1: model for the agent step only ("" = llm_default_model)
    agent_compaction_enabled: bool = True
    agent_compact_above_tokens: int = 6000
    agent_compact_keep_turns: int = 3

    # --- Twilio webhook. Twilio signs the exact public URL it called, so we need it here; with a
    # fake signed request in tests it is just a string. Empty token = every request is rejected. ---
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_webhook_url: str = "http://localhost:8003/webhooks/twilio"
    emergency_classifier: bool = False  # extra LLM check for paraphrases; rules always run first

    # --- Telegram owner bot (long polling: no public URL). The token comes from BotFather; the owner id
    # is an allow-list of ONE person. 0 = nobody is allowed. telegram_live=False means the bot reads
    # updates but only LOGS what it would send (the per-integration dry-run switch). ---
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_owner_id: int = 0
    telegram_live: bool = False
    telegram_poll_s: int = 10  # long-poll wait; also how often alerts and approvals are pushed

    # --- Real integrations (Phase 4). Default = fakes. Each backend is switched on per integration, and
    # real WRITES still obey DRY_RUN. Credentials stay in .env. ---
    calendar_backend: str = "fake"  # fake | google
    google_calendar_id: str = ""  # the TEST calendar's id (share it with the service account's email)
    google_credentials_file: str = ""  # path to the service-account JSON (outside git)
    crm_backend: str = "fake"  # fake | hubspot
    hubspot_token: SecretStr = SecretStr("")  # private-app token
    twilio_live: bool = False  # deliver QUEUED outbox rows through Twilio (needs DRY_RUN=false too)
    twilio_account_sid: str = ""
    twilio_sms_from: str = ""
    twilio_whatsapp_from: str = ""  # sandbox number, e.g. +14155238886
    twilio_allowed_to: str = ""  # comma-separated numbers we may text (your own verified phone)

    # --- Langfuse tracing; with no keys every tracing call quietly does nothing ---
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_host: str = "https://cloud.langfuse.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
