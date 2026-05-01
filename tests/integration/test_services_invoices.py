"""Service-layer tests for app.services.invoices."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.db import session_scope
from app.core.models import Invoice, RawEvent
from app.services import invoices as invoices_service
from app.services.normalization import persist_normalized
from app.utils.hashing import canonical_hash

from ._event_factories import invoice_issued, invoice_paid

pytestmark = pytest.mark.asyncio


async def _seed_invoice() -> uuid.UUID:
    payload = {"doc_ref": "INV-SVC-1", "kind": "issued"}
    async with session_scope() as session:
        raw = RawEvent(payload=payload, vendor_hint="v", hash_exact=canonical_hash(payload))
        session.add(raw)
        await session.flush()
        raw_id = raw.id

    event = invoice_issued(datetime(2026, 4, 15, tzinfo=UTC), document_reference="INV-SVC-1")
    async with session_scope() as session:
        await persist_normalized(session, await session.get(RawEvent, raw_id), event)

    async with session_scope() as session:
        return (await session.execute(select(Invoice.id))).scalar_one()


async def test_get_invoice_returns_loaded_row():
    iid = await _seed_invoice()
    async with session_scope() as session:
        inv = await invoices_service.get_invoice(session, iid)
    assert inv is not None
    assert inv.id == iid
    assert inv.external_ref == "INV-SVC-1"


async def test_get_invoice_returns_None_for_unknown_id():
    async with session_scope() as session:
        inv = await invoices_service.get_invoice(session, uuid.uuid4())
    assert inv is None


async def test_list_events_returns_chronological_order():
    iid = await _seed_invoice()

    second_payload = {"doc_ref": "INV-SVC-1", "kind": "paid"}
    async with session_scope() as session:
        raw = RawEvent(
            payload=second_payload, vendor_hint="v", hash_exact=canonical_hash(second_payload)
        )
        session.add(raw)
        await session.flush()
        raw_id = raw.id

    paid = invoice_paid(
        datetime(2026, 4, 22, 16, 47, 11, tzinfo=UTC), document_reference="INV-SVC-1"
    )
    async with session_scope() as session:
        await persist_normalized(session, await session.get(RawEvent, raw_id), paid)

    async with session_scope() as session:
        events = await invoices_service.list_events(session, iid)
    assert [e.canonical_state for e in events] == ["ISSUED", "PAID"]


async def test_list_events_returns_empty_list_for_unknown_id():
    async with session_scope() as session:
        events = await invoices_service.list_events(session, uuid.uuid4())
    assert events == []
