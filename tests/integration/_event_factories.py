"""Compact factories for typed `NormalizedEvent` values used across integration tests.

Tests should describe the *behavior under test*, not the per-state event shape;
these helpers absorb the boilerplate of constructing the right payload variant.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.events import (
    InvoiceIssuedPayload,
    InvoicePaidPayload,
    InvoiceRefundedPayload,
    InvoiceVoidedPayload,
    NormalizedEvent,
    ShipmentDeliveredPayload,
    ShipmentInTransitPayload,
    ShipmentOutForDeliveryPayload,
    ShipmentPickedUpPayload,
    UnclassifiedPayload,
)

_SHIPMENT_PAYLOAD_BY_STATE: dict[str, type[Any]] = {
    "PICKED_UP": ShipmentPickedUpPayload,
    "IN_TRANSIT": ShipmentInTransitPayload,
    "OUT_FOR_DELIVERY": ShipmentOutForDeliveryPayload,
    "DELIVERED": ShipmentDeliveredPayload,
}


def shipment_event(
    state: str,
    when: datetime,
    *,
    vendor: str = "MAEU",
    transport_doc_number: str = "MAEU240498712",
    container_id: str | None = "MSKU7748112",
    milestone: str | None = None,
) -> NormalizedEvent:
    cls = _SHIPMENT_PAYLOAD_BY_STATE[state]
    return NormalizedEvent(
        vendor=vendor,
        event_at=when,
        payload=cls(
            vendor_milestone=milestone or f"vendor said {state}",
            transport_doc_number=transport_doc_number,
            container_id=container_id,
        ),
    )


def invoice_issued(
    when: datetime,
    *,
    vendor: str = "globalfreightpay",
    document_reference: str = "GFP-INV-1",
    currency: str = "EUR",
    amount: Decimal | None = None,
    issued_at: datetime | None = None,
    milestone: str = "freight invoice raised",
) -> NormalizedEvent:
    return NormalizedEvent(
        vendor=vendor,
        event_at=when,
        payload=InvoiceIssuedPayload(
            vendor_milestone=milestone,
            document_reference=document_reference,
            currency=currency,
            amount=amount or Decimal("100"),
            issued_at=issued_at or when,
        ),
    )


def invoice_paid(
    when: datetime,
    *,
    vendor: str = "globalfreightpay",
    document_reference: str = "GFP-INV-1",
    currency: str = "EUR",
    amount_paid: Decimal | None = None,
    settled_at: datetime | None = None,
    milestone: str = "settled in full",
) -> NormalizedEvent:
    return NormalizedEvent(
        vendor=vendor,
        event_at=when,
        payload=InvoicePaidPayload(
            vendor_milestone=milestone,
            document_reference=document_reference,
            currency=currency,
            amount_paid=amount_paid or Decimal("100"),
            settled_at=settled_at or when,
        ),
    )


def invoice_voided(
    when: datetime,
    *,
    document_reference: str = "GFP-INV-1",
) -> NormalizedEvent:
    return NormalizedEvent(
        vendor="globalfreightpay",
        event_at=when,
        payload=InvoiceVoidedPayload(
            vendor_milestone="cancelled",
            document_reference=document_reference,
            currency="EUR",
            voided_at=when,
        ),
    )


def invoice_refunded(
    when: datetime,
    *,
    document_reference: str = "GFP-INV-1",
) -> NormalizedEvent:
    return NormalizedEvent(
        vendor="globalfreightpay",
        event_at=when,
        payload=InvoiceRefundedPayload(
            vendor_milestone="refund issued",
            document_reference=document_reference,
            currency="EUR",
            refund_amount=Decimal("100"),
            refunded_at=when,
        ),
    )


def unclassified(
    when: datetime,
    *,
    vendor: str = "x",
    reason: str = "unclassified payload",
    extras: dict[str, Any] | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        vendor=vendor,
        event_at=when,
        payload=UnclassifiedPayload(reason=reason, extras=extras or {}),
    )
