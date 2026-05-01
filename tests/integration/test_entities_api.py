"""GET /api/v1/shipments/{id} and /api/v1/invoices/{id} integration tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import create_app
from app.core.db import session_scope
from app.core.models import Invoice, RawEvent, Shipment
from app.services.normalization import persist_normalized
from app.utils.hashing import canonical_hash

from ._event_factories import invoice_issued, invoice_paid, shipment_event

pytestmark = pytest.mark.asyncio


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


async def _new_raw(payload: dict, vendor_hint: str = "v") -> RawEvent:
    async with session_scope() as session:
        re = RawEvent(payload=payload, vendor_hint=vendor_hint, hash_exact=canonical_hash(payload))
        session.add(re)
        await session.flush()
        await session.refresh(re)
        return re


async def _seed_shipment_with_two_events():
    """Seed one shipment with PICKED_UP then IN_TRANSIT."""
    raw1 = await _new_raw({"event_msg_id": "S1"}, vendor_hint="MAEU")
    raw2 = await _new_raw({"event_msg_id": "S2"}, vendor_hint="MAEU")
    t1 = datetime(2026, 4, 19, 11, 0, tzinfo=UTC)
    t2 = datetime(2026, 4, 21, 14, 47, tzinfo=UTC)
    base = shipment_event("PICKED_UP", t1, transport_doc_number="MAEU-API-1")
    later = shipment_event("IN_TRANSIT", t2, transport_doc_number="MAEU-API-1")

    async with session_scope() as session:
        await persist_normalized(session, await session.get(RawEvent, raw1.id), base)
    async with session_scope() as session:
        await persist_normalized(session, await session.get(RawEvent, raw2.id), later)


async def _seed_invoice_with_two_events():
    raw1 = await _new_raw(
        {"doc_ref": "INV-API-1", "kind": "issued"}, vendor_hint="globalfreightpay"
    )
    raw2 = await _new_raw({"doc_ref": "INV-API-1", "kind": "paid"}, vendor_hint="globalfreightpay")
    t_issued = datetime(2026, 4, 15, tzinfo=UTC)
    t_paid = datetime(2026, 4, 22, tzinfo=UTC)
    issued = invoice_issued(t_issued, document_reference="INV-API-1")
    paid = invoice_paid(t_paid, document_reference="INV-API-1")

    async with session_scope() as session:
        await persist_normalized(session, await session.get(RawEvent, raw1.id), issued)
    async with session_scope() as session:
        await persist_normalized(session, await session.get(RawEvent, raw2.id), paid)


async def _shipment_id() -> str:
    from sqlalchemy import select

    async with session_scope() as session:
        return str((await session.execute(select(Shipment.id))).scalar_one())


async def _invoice_id() -> str:
    from sqlalchemy import select

    async with session_scope() as session:
        return str((await session.execute(select(Invoice.id))).scalar_one())


# ---- /shipments/{id} -----------------------------------------------------------


async def test_get_shipment_returns_current_state_and_event_history():
    await _seed_shipment_with_two_events()
    sid = await _shipment_id()

    async with await _client() as ac:
        r = await ac.get(f"/api/v1/shipments/{sid}")

    assert r.status_code == 200
    body = r.json()
    assert body["entity_type"] == "shipment"
    assert body["vendor"] == "MAEU"
    assert body["external_ref"] == "MAEU-API-1"
    assert body["current_state"] == "IN_TRANSIT"
    assert body["last_event_at"].startswith("2026-04-21")
    # Events ordered chronologically.
    assert [e["canonical_state"] for e in body["events"]] == ["PICKED_UP", "IN_TRANSIT"]


async def test_get_shipment_returns_404_for_unknown_id():
    async with await _client() as ac:
        r = await ac.get(f"/api/v1/shipments/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_get_shipment_rejects_invalid_uuid():
    async with await _client() as ac:
        r = await ac.get("/api/v1/shipments/not-a-uuid")
    assert r.status_code == 422


async def test_get_shipment_with_invoice_id_returns_404():
    """A shipment endpoint must not surface an invoice; ids live in different tables."""
    await _seed_invoice_with_two_events()
    iid = await _invoice_id()
    async with await _client() as ac:
        r = await ac.get(f"/api/v1/shipments/{iid}")
    assert r.status_code == 404


# ---- /invoices/{id} ------------------------------------------------------------


async def test_get_invoice_returns_current_state_and_event_history():
    await _seed_invoice_with_two_events()
    iid = await _invoice_id()

    async with await _client() as ac:
        r = await ac.get(f"/api/v1/invoices/{iid}")

    assert r.status_code == 200
    body = r.json()
    assert body["entity_type"] == "invoice"
    assert body["vendor"] == "globalfreightpay"
    assert body["external_ref"] == "INV-API-1"
    assert body["current_state"] == "PAID"
    assert [e["canonical_state"] for e in body["events"]] == ["ISSUED", "PAID"]
    # Latest event is PAID, so entity attributes carry the PAID payload's typed fields.
    assert body["attributes"]["currency"] == "EUR"
    assert body["attributes"]["amount_paid"] == "100"
    # The earlier ISSUED event preserved its own typed fields in history.
    issued = next(e for e in body["events"] if e["canonical_state"] == "ISSUED")
    assert issued["attributes"]["amount"] == "100"


async def test_get_invoice_returns_404_for_unknown_id():
    async with await _client() as ac:
        r = await ac.get(f"/api/v1/invoices/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_get_invoice_with_shipment_id_returns_404():
    await _seed_shipment_with_two_events()
    sid = await _shipment_id()
    async with await _client() as ac:
        r = await ac.get(f"/api/v1/invoices/{sid}")
    assert r.status_code == 404
