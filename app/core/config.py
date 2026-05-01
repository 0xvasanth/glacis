from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: str = "postgresql+asyncpg://glacis:glacis@localhost:5432/glacis"

    llm_model: str = "gemini-2.5-flash"
    google_api_key: str = ""

    worker_poll_interval_s: float = 1.0
    worker_max_attempts: int = 5
    worker_reaper_interval_s: int = 60
    worker_stale_lock_minutes: int = 5
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
