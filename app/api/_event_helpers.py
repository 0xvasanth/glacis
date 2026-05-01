"""Shared helpers for shipment/invoice route handlers."""

from __future__ import annotations

from collections.abc import Iterable

from app.core.models import InvoiceEvent, ShipmentEvent
from app.core.schemas import EventOut


def events_to_out(events: Iterable[ShipmentEvent | InvoiceEvent]) -> list[EventOut]:
    return [
        EventOut(
            id=e.id,
            canonical_state=e.canonical_state,
            event_at=e.event_at,
            vendor_milestone=e.vendor_milestone,
            attributes=e.attributes,
        )
        for e in events
    ]
