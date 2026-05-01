"""Persist a strict typed event into the entity / event tables.

Single transaction, five steps:
  1. (unclassified short-circuit) mark raw_event processed; return.
  2. Upsert the entity by (vendor, external_ref).
  3. Insert the normalized event, deduped by raw_event_id.
  4. Conditional UPDATE on entity: only if event_at is newer than last_event_at.
  5. Mark raw_event processed.

The conditional UPDATE in step 4 implements the "jump to latest, never roll
back" ordering policy: a late event will not overwrite newer state, but its
history row still exists for the audit trail.

The event passed in is one of the typed `NormalizedEvent` variants
(shipment / invoice / unclassified). The schema is enforced by Pydantic at
construction; if a required field is missing, ValidationError fires
upstream (in the worker after the LLM call) so the operator sees exactly
what was missing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.events import (
    NormalizedEvent,
    UnclassifiedPayload,
    _InvoicePayloadBase,
    _ShipmentPayloadBase,
)
from app.core.models import (
    Invoice,
    InvoiceEvent,
    RawEvent,
    Shipment,
    ShipmentEvent,
)


class PersistError(Exception):
    """Raised when a typed event cannot be persisted."""


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _event_attributes(event: NormalizedEvent) -> dict[str, Any]:
    """JSON-friendly dump of the per-state payload.

    Envelope fields (vendor, event_at, vendor_milestone, canonical_state) are
    stored as dedicated columns; the JSONB attributes column carries the
    typed payload's variant-specific fields plus the `extras` lossless bag.
    """
    return event.payload.model_dump(mode="json", exclude_none=False)


async def persist_normalized(
    session: AsyncSession,
    raw_event: RawEvent,
    event: NormalizedEvent,
) -> None:
    now = datetime.now(tz=UTC)

    if isinstance(event.payload, UnclassifiedPayload):
        raw_event.status = "processed"
        raw_event.processed_at = now
        # When the operator opts out of keeping unclassified events, leave a
        # short note so the row is still searchable (status='processed' but
        # last_error explains it was a deliberate drop).
        if get_settings().worker_keep_unclassified_events:
            raw_event.last_error = None
        else:
            raw_event.last_error = f"dropped: unclassified payload ({event.payload.reason})"[:2000]
        await session.flush()
        return

    event_at = _ensure_utc(event.event_at)
    state = event.canonical_state
    # Canonicalize the entity's vendor on `vendor_hint` (URL path, operator-controlled)
    # rather than the LLM-extracted `event.vendor` (free-text, non-deterministic).
    # Two webhooks for the same logical entity that arrive at the same URL path
    # MUST converge to one row even if the LLM picks a different vendor string
    # each time (e.g. "globalfreightpay" vs "HLAG" vs "globalfreightpay.api"
    # for a payload with both `source` and `carrier` fields).
    # The LLM-extracted vendor is still preserved in the event's `attributes`
    # via the typed payload dump, so it remains queryable for analytics.
    vendor = raw_event.vendor_hint
    attributes = _event_attributes(event)

    if isinstance(event.payload, _ShipmentPayloadBase):
        entity_model: type[Shipment] | type[Invoice] = Shipment
        event_model: type[ShipmentEvent] | type[InvoiceEvent] = ShipmentEvent
    elif isinstance(event.payload, _InvoicePayloadBase):
        entity_model = Invoice
        event_model = InvoiceEvent
    else:
        raise PersistError(f"unsupported payload type: {type(event.payload).__name__}")

    # 2. Upsert the entity. The DO UPDATE clause is a no-op-ish bump so that
    # `RETURNING id` always emits a row, whether we inserted or hit the conflict.
    upsert_stmt = (
        pg_insert(entity_model)
        .values(
            vendor=vendor,
            external_ref=event.external_ref,
            attributes=attributes,
        )
        .on_conflict_do_update(
            index_elements=["vendor", "external_ref"],
            set_={"updated_at": now},
        )
        .returning(entity_model.id)
    )
    entity_id: Any = (await session.execute(upsert_stmt)).scalar_one()

    # 3. Insert the normalized event. UNIQUE on raw_event_id makes this
    # idempotent if the worker re-processes the same row after a crash.
    event_stmt = (
        pg_insert(event_model)
        .values(
            entity_id=entity_id,
            raw_event_id=raw_event.id,
            canonical_state=state,
            event_at=event_at,
            vendor_milestone=event.vendor_milestone,
            attributes=attributes,
        )
        .on_conflict_do_nothing(index_elements=["raw_event_id"])
    )
    await session.execute(event_stmt)

    # 4. Conditional state update — the out-of-order guard.
    state_update = (
        update(entity_model)
        .where(entity_model.id == entity_id)
        .where((entity_model.last_event_at.is_(None)) | (entity_model.last_event_at < event_at))
        .values(
            current_state=state,
            last_event_at=event_at,
            attributes=attributes,
            updated_at=now,
        )
    )
    await session.execute(state_update)

    # 5. Mark raw_event processed.
    raw_event.status = "processed"
    raw_event.processed_at = now
    raw_event.last_error = None
    await session.flush()
