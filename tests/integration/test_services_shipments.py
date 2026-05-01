"""Service-layer tests for app.services.shipments.

Exercise the service functions directly with a real session — no HTTP, no
FastAPI. They confirm the contract between the API layer and the data
access layer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.db import session_scope
from app.core.models import RawEvent, Shipment
from app.services import shipments as shipments_service
from app.services.normalization import persist_normalized
from app.utils.hashing import canonical_hash

from ._event_factories import shipment_event

pytestmark = pytest.mark.asyncio


async def _seed_shipment(canonical_state: str = "PICKED_UP") -> uuid.UUID:
    payload = {"event_msg_id": f"S-{canonical_state}"}
    async with session_scope() as session:
        raw = RawEvent(payload=payload, vendor_hint="v", hash_exact=canonical_hash(payload))
        session.add(raw)
        await session.flush()
        raw_id = raw.id

    event = shipment_event(
        canonical_state,
        datetime(2026, 4, 19, 11, 0, tzinfo=UTC),
        transport_doc_number="MAEU-SVC-1",
    )
    async with session_scope() as session:
        await persist_normalized(session, await session.get(RawEvent, raw_id), event)

    async with session_scope() as session:
        return (await session.execute(select(Shipment.id))).scalar_one()


async def test_get_shipment_returns_loaded_row():
    sid = await _seed_shipment()
    async with session_scope() as session:
        ship = await shipments_service.get_shipment(session, sid)
    assert ship is not None
    assert ship.id == sid
    assert ship.external_ref == "MAEU-SVC-1"


async def test_get_shipment_returns_None_for_unknown_id():
    async with session_scope() as session:
        ship = await shipments_service.get_shipment(session, uuid.uuid4())
    assert ship is None


async def test_list_events_returns_chronological_order():
    """Insert events out of order; service must return them by event_at ASC."""
    sid = await _seed_shipment("PICKED_UP")

    # Add a second event with a later timestamp.
    second_payload = {"event_msg_id": "S-2"}
    async with session_scope() as session:
        raw = RawEvent(
            payload=second_payload, vendor_hint="v", hash_exact=canonical_hash(second_payload)
        )
        session.add(raw)
        await session.flush()
        raw_id = raw.id

    later = shipment_event(
        "DELIVERED",
        datetime(2026, 4, 28, 9, 42, tzinfo=UTC),
        transport_doc_number="MAEU-SVC-1",
    )
    async with session_scope() as session:
        await persist_normalized(session, await session.get(RawEvent, raw_id), later)

    async with session_scope() as session:
        events = await shipments_service.list_events(session, sid)
    assert [e.canonical_state for e in events] == ["PICKED_UP", "DELIVERED"]
    assert events[0].event_at < events[1].event_at


async def test_list_events_returns_empty_list_for_unknown_id():
    async with session_scope() as session:
        events = await shipments_service.list_events(session, uuid.uuid4())
    assert events == []
