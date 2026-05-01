"""Persist an incoming raw event with hash-based duplicate detection.

Always inserts a row — even duplicates land in the table for full audit.
A duplicate row carries `status='duplicate'` and `duplicate_of_id` pointing
at the canonical parent, so it is invisible to the worker (which only
claims `status='pending'`) but visible in admin / metrics queries.

Concurrency: the partial unique index `uq_raw_events_primary_per_hash`
(`WHERE duplicate_of_id IS NULL`) guarantees at most one PRIMARY row per
`(vendor_hint, hash_exact)`. We try the primary INSERT first; on
ON CONFLICT DO NOTHING we look up the surviving primary and write a
duplicate row pointing at it. This closes the SELECT-then-INSERT TOCTOU
race that would otherwise let two concurrent identical POSTs both become
primaries (and both pay the LLM).
"""

from __future__ import annotations

import uuid
from typing import Any, NamedTuple

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
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

    # Phase 1: try to insert as a primary. The partial unique index makes
    # this atomic — if a primary already exists, ON CONFLICT skips the
    # insert and we get None back from RETURNING.
    primary_stmt = (
        pg_insert(RawEvent)
        .values(
            vendor_hint=vendor_hint,
            payload=payload,
            hash_exact=h,
            duplicate_of_id=None,
        )
        .on_conflict_do_nothing(
            index_elements=["vendor_hint", "hash_exact"],
            index_where=text("duplicate_of_id IS NULL"),
        )
        .returning(RawEvent.id)
    )
    primary_id = (await session.execute(primary_stmt)).scalar_one_or_none()
    if primary_id is not None:
        return IngestResult(raw_event_id=primary_id, duplicate=False, duplicate_of=None)

    # Phase 2: a primary already exists; look it up.
    parent_id: uuid.UUID = (
        await session.execute(
            select(RawEvent.id)
            .where(RawEvent.vendor_hint == vendor_hint)
            .where(RawEvent.hash_exact == h)
            .where(RawEvent.duplicate_of_id.is_(None))
            .order_by(RawEvent.received_at.asc())
            .limit(1)
        )
    ).scalar_one()

    # Phase 3: insert as a duplicate row pointing at the parent.
    duplicate_row = RawEvent(
        vendor_hint=vendor_hint,
        payload=payload,
        hash_exact=h,
        duplicate_of_id=parent_id,
        status="duplicate",
    )
    session.add(duplicate_row)
    await session.flush()

    return IngestResult(
        raw_event_id=duplicate_row.id,
        duplicate=True,
        duplicate_of=parent_id,
    )
