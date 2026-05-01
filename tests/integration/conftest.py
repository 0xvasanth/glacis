"""Test environment for integration tests.

Approach: configure DATABASE_URL as an environment variable *before* the
application code runs, then clear the settings cache so `get_settings()`
re-reads from the env. The app's lazy engine factory then initializes
against the testcontainer URL on first use — no monkeypatching of module
internals required. Tests that need an LLM stub the classifier directly.

The schema is created once per pytest session via the shared `engine`
fixture; data is cleared between tests via TRUNCATE.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer


def _to_asyncpg(url: str) -> str:
    """testcontainers returns a psycopg URL; convert to asyncpg."""
    return (
        url.replace("psycopg2", "asyncpg")
        .replace("postgresql+psycopg", "postgresql+asyncpg")
        .replace("postgresql://", "postgresql+asyncpg://")
    )


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Spin up a Postgres container for the entire test session."""
    with PostgresContainer("postgres:16-alpine") as pg:
        yield _to_asyncpg(pg.get_connection_url())


@pytest.fixture(scope="session", autouse=True)
def configure_app_env(postgres_url: str) -> Iterator[None]:
    """Bind the app to the test Postgres.

    Runs once per session, BEFORE any test fixture that imports app code.
    Clears the cached Settings so subsequent `get_settings()` reads the env.
    Tests stub the classifier directly — no LLM is constructed.
    """
    os.environ["DATABASE_URL"] = postgres_url

    from app.core.config import get_settings

    get_settings.cache_clear()
    yield


@pytest_asyncio.fixture(scope="session")
async def engine(postgres_url: str, configure_app_env: None) -> AsyncIterator[AsyncEngine]:
    """A session-scoped engine used only for schema setup and cleanup.

    Production code uses its own engine (initialized lazily via
    `app.core.db.get_engine()`); both bind to the same testcontainer Postgres
    so they share data.
    """
    eng = create_async_engine(postgres_url, future=True)
    async with eng.begin() as conn:
        await conn.exec_driver_sql('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
        from app.core.models import Base

        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_db(engine: AsyncEngine) -> AsyncIterator[None]:
    """Truncate every domain table between tests so each starts clean."""
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "TRUNCATE TABLE shipment_events, invoice_events, shipments, invoices, "
            "raw_events RESTART IDENTITY CASCADE"
        )
    yield


@pytest_asyncio.fixture(scope="session", autouse=True)
async def dispose_app_engine_at_session_end() -> AsyncIterator[None]:
    """Close the app's lazy engine when the test session finishes."""
    yield
    from app.core.db import dispose_engine

    await dispose_engine()
