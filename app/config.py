"""All runtime configuration, read from the environment (see .env.example)."""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: str = "postgresql://app:change-me-local-only@localhost:5455/gateway"

    whatsapp_app_secret: str = "replace-with-meta-app-secret"
    whatsapp_verify_token: str = "replace-with-verify-token"
    whatsapp_mode: Literal["mock", "graph"] = "mock"
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    max_body_bytes: int = 256 * 1024

    admin_token: str = "replace-with-long-random-admin-token"
    allow_reset: bool = False

    lab_base_url: str = "http://mocklab:8100"
    lab_name: str = "Synthetic Diagnostics"
    lab_timeout_s: float = 3.0
    lab_ambiguous_margin_s: float = 0.5
    lab_max_attempts: int = 4
    lab_backoff_base_s: float = 0.2
    lab_backoff_cap_s: float = 3.0
    cb_failure_threshold: int = 5
    cb_open_seconds: float = 30.0

    rate_limit_capacity: float = 6
    rate_limit_refill_per_sec: float = 0.1
    rate_limit_notice_window_s: float = 60

    llm_mode: Literal["mock", "real"] = "mock"
    # gemini = Gemini's OpenAI-compatible endpoint (adapter applies Gemini-specific request tweaks)
    llm_provider: Literal["anthropic", "openai", "gemini"] = "gemini"
    llm_model: str = "gemini-3.1-flash-lite"
    llm_base_url: str = ""
    # API_KEY is accepted as an alias. repr=False keeps it out of reprs and validation errors.
    llm_api_key: str = Field(default="", repr=False, validation_alias=AliasChoices("LLM_API_KEY", "API_KEY"))
    # free | paid: the key's billing tier, used only to report billed vs paid-equivalent spend.
    llm_billing_tier: Literal["free", "paid"] = "free"
    llm_max_retries: int = 4  # provider 429/5xx retries per call (honours Retry-After)
    llm_budget_usd: float = 2.0
    llm_max_calls_per_turn: int = 4
    llm_history_turns: int = 8
    llm_max_output_tokens: int = 1024
    llm_timeout_s: float = 30.0
    mock_price_model: str = "claude-haiku-5-5"
    usd_inr: float = 88.0

    worker_concurrency: int = 8
    lease_seconds: float = 60
    heartbeat_seconds: float = 20
    max_message_attempts: int = 5
    poll_interval_s: float = 0.2
    reconcile_interval_s: float = 15
    booking_unknown_handoff_s: float = 600
    max_failed_bookings: int = 3

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
