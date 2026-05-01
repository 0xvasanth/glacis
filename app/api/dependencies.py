"""Shared FastAPI dependencies.

Centralizes the session-scope dependency so route handlers stay thin and
service-layer functions can be unit-tested by passing any AsyncSession.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_scope


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session
