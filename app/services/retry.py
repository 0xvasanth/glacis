"""Operator-triggered manual retry for a single raw_event.

Synchronous: claim the row, call the LLM, persist, return the updated
`RawEvent`. Use this when a row is stuck (`status='processing'` beyond the
reaper window) or has been parked at `attempts == max`.

Transactions are managed internally in three phases:

  1. **Acquire** — a short tx that locks the row, validates state, sets
     `status='processing'`, increments `attempts`, clears `last_error`.
     Committing this phase guarantees the attempts bump is recorded
     regardless of what happens later, and the lock keeps the worker's
     `SKIP LOCKED` claim away from us.

  2. **Process** — classify + persist in their own tx. Commits on
     success; on exception the tx rolls back (no partial entity writes).

  3. **Record outcome** — only on phase-2 failure. A fresh tx writes
     `status='pending'` and `last_error` so the row is searchable and
     a future retry / worker run can pick it back up.

Splitting the phases this way lets failures cleanly persist their
diagnostic state instead of being silently rolled back with the
processing tx.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.db import session_scope
from app.core.models import RawEvent
from app.llm.classifier import SupportsClassify
from app.services.normalization import persist_normalized


class RetryNotFoundError(Exception):
    """The raw_event_id does not exist."""


class RetryRefusedError(Exception):
    """Row exists but is in a state where retry is meaningless."""

    def __init__(self, status: str, reason: str):
        self.status = status
        self.reason = reason
        super().__init__(reason)


async def retry_raw_event(
    classifier: SupportsClassify,
    raw_event_id: uuid.UUID,
) -> RawEvent:
    """Re-run classification + persistence for a single raw_event.

    Raises:
      RetryNotFoundError — id does not exist.
      RetryRefusedError  — row is a duplicate, or already processed (a re-run
                      would be a no-op because of *_events.raw_event_id UNIQUE).
      Anything else — propagated from the classifier or persist layer.
                      The row will be left at status='pending' with
                      `last_error` set so it remains searchable.
    """
    # Phase 1: acquire (own tx — commits the lock + attempts bump)
    async with session_scope() as session:
        raw = (
            await session.execute(
                select(RawEvent).where(RawEvent.id == raw_event_id).with_for_update()
            )
        ).scalar_one_or_none()
        if raw is None:
            raise RetryNotFoundError(str(raw_event_id))
        if raw.status == "duplicate":
            raise RetryRefusedError(
                "duplicate",
                f"row is a duplicate of {raw.duplicate_of_id}; retry the parent instead",
            )
        if raw.status == "processed":
            raise RetryRefusedError(
                "processed",
                "row is already processed; nothing to retry",
            )
        raw.status = "processing"
        raw.locked_at = datetime.now(UTC)
        raw.attempts = (raw.attempts or 0) + 1
        raw.last_error = None
        payload = raw.payload  # snapshot before the session closes

    # Phase 2: classify + persist (own tx)
    try:
        result = await classifier.classify(payload)
        async with session_scope() as session:
            row = await session.get(RawEvent, raw_event_id)
            if row is None:  # extremely defensive: deleted between phases
                raise RetryNotFoundError(str(raw_event_id))
            await persist_normalized(session, row, result)
    except Exception as exc:
        # Phase 3: record outcome (own tx). Rolls back nothing the worker
        # can't recover; just preserves diagnostic state.
        async with session_scope() as session:
            row = await session.get(RawEvent, raw_event_id)
            if row is not None:
                row.status = "pending"
                row.last_error = f"manual retry: {exc!r}"[:2000]
        raise

    # Phase 4: read final state for the caller
    async with session_scope() as session:
        row = await session.get(RawEvent, raw_event_id)
        if row is None:
            raise RetryNotFoundError(str(raw_event_id))
        return row
