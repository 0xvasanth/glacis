from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProvider = Literal["google", "anthropic"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: str = "postgresql+asyncpg://glacis:glacis@localhost:5432/glacis"

    llm_provider: LLMProvider = Field(
        default="google",
        description="Which LLM backend to use. 'google' (Gemini) is cheap but its "
        "responseSchema has partial oneOf support — discriminated unions can drop "
        "per-variant required fields. 'anthropic' (Claude) honors the union schema "
        "fully via tool-use. Recommended for production where strict schema "
        "enforcement matters more than per-token cost.",
    )
    llm_model: str = "gemini-2.5-flash"
    google_api_key: str = ""
    anthropic_api_key: str = ""

    worker_poll_interval_s: float = 1.0
    worker_max_attempts: int = Field(
        default=5,
        description="Per-event LLM-failure budget. Crashes don't consume it (claim_one "
        "doesn't increment); only mark_failed does. After max_attempts the row "
        "moves to the terminal 'failed' status (DLQ).",
    )
    worker_reaper_interval_s: int = 60
    worker_stale_lock_minutes: int = Field(
        default=5,
        description="How old `locked_at` must be before the reaper resets a 'processing' "
        "row to 'pending'. Bound: must comfortably exceed the worst-case in-flight "
        "duration. With Gemini timeout=30s x max_retries=2 → ~90s LLM, plus persist "
        "tx ~1s, the worst case is ~100s. Default 5 min gives a 3x margin. "
        "If you raise the LLM timeout in the LangChain client, raise this too.",
    )
    worker_keep_unclassified_events: bool = Field(
        default=True,
        description="If True, UNCLASSIFIED events are recorded in the audit log; "
        "if False, the worker marks the raw_event 'processed' and notes "
        "'dropped: unclassified payload' in last_error.",
    )

    log_level: str = "INFO"

    api_request_max_bytes: int = Field(default=1024 * 1024, description="1 MiB body cap")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
