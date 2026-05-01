"""Tests the heart of the system: out-of-order safety, idempotency, and the
shipment / invoice transition lifecycle.

Uses typed `NormalizedEvent` values via factories in `_event_factories`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.db import session_scope
from app.core.models import (
    Invoice,
    InvoiceEvent,
    RawEvent,
    Shipment,
    ShipmentEvent,
)
from app.services.normalization import persist_normalized
from app.utils.hashing import canonical_hash

from ._event_factories import (
    invoice_issued,
    invoice_paid,
    invoice_refunded,
    invoice_voided,
    shipment_event,
    unclassified,
)

pytestmark = pytest.mark.asyncio


async def _new_raw(payload: dict, vendor_hint: str = "v") -> RawEvent:
    async with session_scope() as session:
        re = RawEvent(
            payload=payload,
            vendor_hint=vendor_hint,
            hash_exact=canonical_hash(payload),
        )
        session.add(re)
        await session.flush()
        await session.refresh(re)
        return re


# ----------------------------------------------------------------------------
# Unclassified short-circuit
# ----------------------------------------------------------------------------


async def test_unclassified_marks_processed_and_creates_no_entity():
    raw = await _new_raw({"foo": "bar"})
    event = unclassified(datetime(2026, 4, 26, tzinfo=UTC))
    async with session_scope() as session:
        await persist_normalized(session, await session.get(RawEvent, raw.id), event)

    async with session_scope() as session:
        re = await session.get(RawEvent, raw.id)
        assert re is not None and re.status == "processed"
        assert (await session.execute(select(Shipment))).scalars().all() == []
        assert (await session.execute(select(Invoice))).scalars().all() == []


# ----------------------------------------------------------------------------
# Shipment lifecycle: in-order, out-of-order, and skip
# ----------------------------------------------------------------------------


async def test_two_events_same_shipment_link_to_one_entity():
    raw1 = await _new_raw({"event_msg_id": "E1"})
    raw2 = await _new_raw({"event_msg_id": "E2"})
    t1 = datetime(2026, 4, 19, 11, 15, tzinfo=UTC)
    t2 = datetime(2026, 4, 21, 14, 47, tzinfo=UTC)

    async with session_scope() as session:
        await persist_normalized(
            session, await session.get(RawEvent, raw1.id), shipment_event("PICKED_UP", t1)
        )
    async with session_scope() as session:
        await persist_normalized(
            session, await session.get(RawEvent, raw2.id), shipment_event("IN_TRANSIT", t2)
        )

    async with session_scope() as session:
        ships = (await session.execute(select(Shipment))).scalars().all()
        assert len(ships) == 1
        assert ships[0].current_state == "IN_TRANSIT"
        assert ships[0].last_event_at == t2
        events = (
            (await session.execute(select(ShipmentEvent).order_by(ShipmentEvent.event_at)))
            .scalars()
            .all()
        )
        assert [e.canonical_state for e in events] == ["PICKED_UP", "IN_TRANSIT"]


async def test_out_of_order_does_not_overwrite_newer_state():
    """Late older event MUST NOT regress current_state, but its history row exists."""
    raw_new = await _new_raw({"event_msg_id": "NEW"})
    raw_old = await _new_raw({"event_msg_id": "OLD"})
    t_new = datetime(2026, 4, 28, 9, 42, tzinfo=UTC)
    t_old = datetime(2026, 4, 19, 11, 15, tzinfo=UTC)

    async with session_scope() as session:
        await persist_normalized(
            session, await session.get(RawEvent, raw_new.id), shipment_event("DELIVERED", t_new)
        )
    async with session_scope() as session:
        await persist_normalized(
            session, await session.get(RawEvent, raw_old.id), shipment_event("PICKED_UP", t_old)
        )

    async with session_scope() as session:
        ship = (await session.execute(select(Shipment))).scalar_one()
        assert ship.current_state == "DELIVERED"
        assert ship.last_event_at == t_new
        events = (
            (await session.execute(select(ShipmentEvent).order_by(ShipmentEvent.event_at)))
            .scalars()
            .all()
        )
        assert [e.canonical_state for e in events] == ["PICKED_UP", "DELIVERED"]


async def test_three_events_arriving_random_order_resolves_correctly():
    raws = [await _new_raw({"event_msg_id": f"E{i}"}) for i in range(3)]
    t_picked = datetime(2026, 4, 19, 11, 0, tzinfo=UTC)
    t_transit = t_picked + timedelta(days=2)
    t_delivered = t_picked + timedelta(days=10)

    plan = [
        (raws[0], shipment_event("DELIVERED", t_delivered)),
        (raws[1], shipment_event("PICKED_UP", t_picked)),
        (raws[2], shipment_event("IN_TRANSIT", t_transit)),
    ]
    for raw, event in plan:
        async with session_scope() as session:
            await persist_normalized(session, await session.get(RawEvent, raw.id), event)

    async with session_scope() as session:
        ship = (await session.execute(select(Shipment))).scalar_one()
        assert ship.current_state == "DELIVERED"
        assert ship.last_event_at == t_delivered


async def test_full_lifecycle_in_order_walks_through_all_4_states():
    raws = [await _new_raw({"event_msg_id": f"L{i}"}) for i in range(4)]
    base = datetime(2026, 4, 19, tzinfo=UTC)
    states = ["PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED"]
    timestamps = [base + timedelta(days=i * 2) for i in range(4)]

    for raw, state, ts in zip(raws, states, timestamps, strict=True):
        async with session_scope() as session:
            await persist_normalized(
                session, await session.get(RawEvent, raw.id), shipment_event(state, ts)
            )

    async with session_scope() as session:
        ship = (await session.execute(select(Shipment))).scalar_one()
        assert ship.current_state == "DELIVERED"
        assert ship.last_event_at == timestamps[-1]
        events = (
            (await session.execute(select(ShipmentEvent).order_by(ShipmentEvent.event_at)))
            .scalars()
            .all()
        )
        assert [e.canonical_state for e in events] == states


async def test_full_lifecycle_in_REVERSE_arrival_order_still_settles_at_DELIVERED():
    raws = [await _new_raw({"event_msg_id": f"R{i}"}) for i in range(4)]
    base = datetime(2026, 4, 19, tzinfo=UTC)
    states_chrono = ["PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED"]
    timestamps_chrono = [base + timedelta(days=i * 2) for i in range(4)]

    for raw, state, ts in zip(
        raws, list(reversed(states_chrono)), list(reversed(timestamps_chrono)), strict=True
    ):
        async with session_scope() as session:
            await persist_normalized(
                session, await session.get(RawEvent, raw.id), shipment_event(state, ts)
            )

    async with session_scope() as session:
        ship = (await session.execute(select(Shipment))).scalar_one()
        assert ship.current_state == "DELIVERED"
        assert ship.last_event_at == timestamps_chrono[-1]
        events = (
            (await session.execute(select(ShipmentEvent).order_by(ShipmentEvent.event_at)))
            .scalars()
            .all()
        )
        assert [e.canonical_state for e in events] == states_chrono


async def test_skipping_intermediate_states_is_allowed():
    raw1 = await _new_raw({"event_msg_id": "S1"})
    raw2 = await _new_raw({"event_msg_id": "S2"})
    t1 = datetime(2026, 4, 19, tzinfo=UTC)
    t2 = datetime(2026, 4, 28, tzinfo=UTC)

    async with session_scope() as session:
        await persist_normalized(
            session, await session.get(RawEvent, raw1.id), shipment_event("PICKED_UP", t1)
        )
    async with session_scope() as session:
        await persist_normalized(
            session, await session.get(RawEvent, raw2.id), shipment_event("DELIVERED", t2)
        )

    async with session_scope() as session:
        ship = (await session.execute(select(Shipment))).scalar_one()
        assert ship.current_state == "DELIVERED"


async def test_late_arriving_in_transit_after_out_for_delivery_does_NOT_regress():
    raw_ofd = await _new_raw({"event_msg_id": "OFD"})
    raw_it = await _new_raw({"event_msg_id": "IT"})
    t_in_transit = datetime(2026, 4, 21, tzinfo=UTC)
    t_out_for_delivery = datetime(2026, 4, 27, tzinfo=UTC)

    async with session_scope() as session:
        await persist_normalized(
            session,
            await session.get(RawEvent, raw_ofd.id),
            shipment_event("OUT_FOR_DELIVERY", t_out_for_delivery),
        )
    async with session_scope() as session:
        await persist_normalized(
            session,
            await session.get(RawEvent, raw_it.id),
            shipment_event("IN_TRANSIT", t_in_transit),
        )

    async with session_scope() as session:
        ship = (await session.execute(select(Shipment))).scalar_one()
        assert ship.current_state == "OUT_FOR_DELIVERY"
        assert ship.last_event_at == t_out_for_delivery
        events = (
            (await session.execute(select(ShipmentEvent).order_by(ShipmentEvent.event_at)))
            .scalars()
            .all()
        )
        assert [e.canonical_state for e in events] == ["IN_TRANSIT", "OUT_FOR_DELIVERY"]


async def test_same_event_at_does_not_overwrite_state():
    raw1 = await _new_raw({"event_msg_id": "T1"})
    raw2 = await _new_raw({"event_msg_id": "T2"})
    t = datetime(2026, 4, 21, 14, 47, tzinfo=UTC)

    async with session_scope() as session:
        await persist_normalized(
            session, await session.get(RawEvent, raw1.id), shipment_event("PICKED_UP", t)
        )
    async with session_scope() as session:
        await persist_normalized(
            session, await session.get(RawEvent, raw2.id), shipment_event("DELIVERED", t)
        )

    async with session_scope() as session:
        ship = (await session.execute(select(Shipment))).scalar_one()
        assert ship.current_state == "PICKED_UP"
        assert len((await session.execute(select(ShipmentEvent))).scalars().all()) == 2


# ----------------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------------


async def test_double_processing_same_raw_event_is_idempotent():
    raw = await _new_raw({"event_msg_id": "DUP"})
    t = datetime(2026, 4, 21, 14, 47, tzinfo=UTC)
    event = shipment_event("IN_TRANSIT", t)

    async with session_scope() as session:
        await persist_normalized(session, await session.get(RawEvent, raw.id), event)
    async with session_scope() as session:
        re = await session.get(RawEvent, raw.id)
        assert re is not None
        re.status = "processing"
    async with session_scope() as session:
        await persist_normalized(session, await session.get(RawEvent, raw.id), event)

    async with session_scope() as session:
        events = (await session.execute(select(ShipmentEvent))).scalars().all()
        assert len(events) == 1


async def test_naive_event_at_is_treated_as_utc():
    raw = await _new_raw({"x": 1})
    naive = datetime(2026, 4, 21, 14, 47)  # no tzinfo
    event = shipment_event("IN_TRANSIT", naive)
    async with session_scope() as session:
        await persist_normalized(session, await session.get(RawEvent, raw.id), event)
    async with session_scope() as session:
        ev = (await session.execute(select(ShipmentEvent))).scalar_one()
        assert ev.event_at == datetime(2026, 4, 21, 14, 47, tzinfo=UTC)


# ----------------------------------------------------------------------------
# Invoice lifecycles + terminal alternatives
# ----------------------------------------------------------------------------


async def test_invoice_lifecycle_issued_then_paid():
    raw1 = await _new_raw({"doc_ref": "INV-1", "kind": "issued"})
    raw2 = await _new_raw({"doc_ref": "INV-1", "kind": "paid"})
    t_issued = datetime(2026, 4, 15, 7, 0, tzinfo=UTC)
    t_paid = datetime(2026, 4, 22, 16, 47, 11, tzinfo=UTC)

    async with session_scope() as session:
        await persist_normalized(
            session, await session.get(RawEvent, raw1.id), invoice_issued(t_issued)
        )
    async with session_scope() as session:
        await persist_normalized(
            session, await session.get(RawEvent, raw2.id), invoice_paid(t_paid)
        )

    async with session_scope() as session:
        inv = (await session.execute(select(Invoice))).scalar_one()
        assert inv.current_state == "PAID"
        assert inv.last_event_at == t_paid
        events = (
            (await session.execute(select(InvoiceEvent).order_by(InvoiceEvent.event_at)))
            .scalars()
            .all()
        )
        assert [e.canonical_state for e in events] == ["ISSUED", "PAID"]


async def test_invoice_voided_after_issued_is_terminal():
    raw1 = await _new_raw({"doc_ref": "INV-X", "kind": "issued"})
    raw2 = await _new_raw({"doc_ref": "INV-X", "kind": "voided"})
    t_issued = datetime(2026, 4, 15, tzinfo=UTC)
    t_voided = datetime(2026, 4, 17, tzinfo=UTC)

    async with session_scope() as session:
        await persist_normalized(
            session, await session.get(RawEvent, raw1.id), invoice_issued(t_issued)
        )
    async with session_scope() as session:
        await persist_normalized(
            session, await session.get(RawEvent, raw2.id), invoice_voided(t_voided)
        )

    async with session_scope() as session:
        inv = (await session.execute(select(Invoice))).scalar_one()
        assert inv.current_state == "VOIDED"


async def test_invoice_refunded_after_paid_is_terminal():
    raws = [await _new_raw({"doc_ref": "INV-X", "kind": s}) for s in ("issued", "paid", "refunded")]
    timestamps = [
        datetime(2026, 4, 15, tzinfo=UTC),
        datetime(2026, 4, 22, tzinfo=UTC),
        datetime(2026, 4, 25, tzinfo=UTC),
    ]
    events = [
        invoice_issued(timestamps[0]),
        invoice_paid(timestamps[1]),
        invoice_refunded(timestamps[2]),
    ]
    states = ["ISSUED", "PAID", "REFUNDED"]

    for raw, ev in zip(raws, events, strict=True):
        async with session_scope() as session:
            await persist_normalized(session, await session.get(RawEvent, raw.id), ev)

    async with session_scope() as session:
        inv = (await session.execute(select(Invoice))).scalar_one()
        assert inv.current_state == "REFUNDED"
        assert inv.last_event_at == timestamps[-1]
        history = (
            (await session.execute(select(InvoiceEvent).order_by(InvoiceEvent.event_at)))
            .scalars()
            .all()
        )
        assert [e.canonical_state for e in history] == states


async def test_invoice_paid_then_late_issued_does_NOT_regress():
    raw_paid = await _new_raw({"doc_ref": "INV-X", "kind": "paid"})
    raw_issued = await _new_raw({"doc_ref": "INV-X", "kind": "issued"})
    t_issued = datetime(2026, 4, 15, tzinfo=UTC)
    t_paid = datetime(2026, 4, 22, tzinfo=UTC)

    async with session_scope() as session:
        await persist_normalized(
            session, await session.get(RawEvent, raw_paid.id), invoice_paid(t_paid)
        )
    async with session_scope() as session:
        await persist_normalized(
            session, await session.get(RawEvent, raw_issued.id), invoice_issued(t_issued)
        )

    async with session_scope() as session:
        inv = (await session.execute(select(Invoice))).scalar_one()
        assert inv.current_state == "PAID"
        assert inv.last_event_at == t_paid
