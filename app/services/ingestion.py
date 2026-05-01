"""Persist an incoming raw event with hash-based duplicate detection.

Always inserts a row — even duplicates land in the table for full audit.
A duplicate row carries `status='duplicate'` and `duplicate_of_id` pointing
at the canonical parent, so it is invisible to the worker (which only
claims `status='pending'`) but visible in admin / metrics queries.
"""

from __future__ import annotations

import uuid
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import RawEvent
from app.utils.hashing import canonical_hash


class IngestResult(NamedTuple):
    raw_event_id: uuid.UUID
    duplicate: bool
    duplicate_of: uuid.UUID | None


async def insert_raw_event(
    session: AsyncSession,
    *,
    vendor_hint: str,
    payload: dict[str, Any],
) -> IngestResult:
    """Insert the raw event, marking it a duplicate if a parent already exists."""
    h = canonical_hash(payload)

    parent_id = (
        await session.execute(
            select(RawEvent.id)
            .where(RawEvent.vendor_hint == vendor_hint)
            .where(RawEvent.hash_exact == h)
            .where(RawEvent.duplicate_of_id.is_(None))  # only follow chains one level deep
            .order_by(RawEvent.received_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    row = RawEvent(
        vendor_hint=vendor_hint,
        payload=payload,
        hash_exact=h,
        duplicate_of_id=parent_id,
        status="duplicate" if parent_id is not None else "pending",
    )
    session.add(row)
    await session.flush()

    return IngestResult(
        raw_event_id=row.id,
        duplicate=parent_id is not None,
        duplicate_of=parent_id,
    )
