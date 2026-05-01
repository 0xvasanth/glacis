"""Shared FastAPI dependencies.

Centralizes the session-scope and classifier dependencies so route
handlers stay thin and service-layer functions can be unit-tested by
passing the dependencies directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import session_scope
from app.llm import build_llm
from app.llm.classifier import Classifier


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


@lru_cache(maxsize=1)
def _build_classifier() -> Classifier:
    """Single Classifier per process — building the LLM client is expensive."""
    return Classifier(build_llm(get_settings()))


def get_classifier() -> Classifier:
    return _build_classifier()
