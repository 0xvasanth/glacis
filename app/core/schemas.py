"""API request/response DTOs and shared canonical-state constants.

The LLM-facing event schema lives in `app.core.events`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

EntityType = Literal["shipment", "invoice", "unclassified"]

SHIPMENT_STATES: tuple[str, ...] = ("PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED")
INVOICE_STATES: tuple[str, ...] = ("ISSUED", "PAID", "VOIDED", "REFUNDED")
ALL_CANONICAL_STATES: tuple[str, ...] = (*SHIPMENT_STATES, *INVOICE_STATES)


class WebhookAck(BaseModel):
    raw_event_id: uuid.UUID
    duplicate: bool
    duplicate_of: uuid.UUID | None = None


class RawEventRetryResult(BaseModel):
    raw_event_id: uuid.UUID
    status: str
    attempts: int
    canonical_state: str | None
    entity_type: str | None
    last_error: str | None


class EventOut(BaseModel):
    id: uuid.UUID
    canonical_state: str
    event_at: datetime
    vendor_milestone: str | None
    attributes: dict[str, Any]


class ShipmentOut(BaseModel):
    id: uuid.UUID
    entity_type: Literal["shipment"] = "shipment"
    vendor: str
    external_ref: str
    current_state: str | None
    last_event_at: datetime | None
    attributes: dict[str, Any]
    events: list[EventOut]


class InvoiceOut(BaseModel):
    id: uuid.UUID
    entity_type: Literal["invoice"] = "invoice"
    vendor: str
    external_ref: str
    current_state: str | None
    last_event_at: datetime | None
    attributes: dict[str, Any]
    events: list[EventOut]
