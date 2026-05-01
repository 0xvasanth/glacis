"""Typed event schema — the internal contract between the LLM and our platform."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Payload base classes
# ---------------------------------------------------------------------------


class _PayloadBase(BaseModel):
    """Common config across every payload variant.

    Every payload carries an `extras` bag — a free-form dict where the LLM
    can stash any vendor-specific fields the payload contained that don't
    map cleanly onto our typed schema. We keep this so no source data is
    lost in normalization; downstream systems can query `extras` later
    without reparsing the raw event. Use sparingly for shipment/invoice
    payloads (most useful info is already typed); for UNCLASSIFIED it is
    the primary place vendor data lands.
    """

    model_config = ConfigDict(extra="forbid")

    extras: dict[str, Any] = Field(
        default_factory=dict,
        description="Vendor-specific fields not modeled by the typed schema. "
        "Preserved verbatim so future analytics / downstream systems can use them.",
    )


class _ShipmentPayloadBase(_PayloadBase):
    """Fields every SHIPMENT payload requires."""

    entity_type: Literal["shipment"] = "shipment"
    vendor_milestone: str = Field(
        min_length=1, description="Original vendor wording for the event (audit trail)."
    )
    transport_doc_number: str = Field(
        min_length=1,
        description="MBL / HBL / AWB number — the entity's natural key. Prefer Master BL.",
    )
    container_id: str | None = Field(default=None, description="Container number if present.")


class _InvoicePayloadBase(_PayloadBase):
    """Fields every INVOICE payload requires."""

    entity_type: Literal["invoice"] = "invoice"
    vendor_milestone: str = Field(min_length=1)
    document_reference: str = Field(
        min_length=1, description="Invoice / document reference — the entity's natural key."
    )
    currency: str = Field(min_length=3, max_length=3, description="ISO-4217 code, e.g. 'EUR'.")
    payer: str | None = None
    payee: str | None = None


# ---------------------------------------------------------------------------
# Shipment payloads
# ---------------------------------------------------------------------------


class ShipmentPickedUpPayload(_ShipmentPayloadBase):
    canonical_state: Literal["PICKED_UP"] = "PICKED_UP"

    origin_port_code: str | None = None
    origin_port_name: str | None = None
    shipper_reference: str | None = Field(default=None, description="Shipper-side PO / ref.")


class ShipmentInTransitPayload(_ShipmentPayloadBase):
    canonical_state: Literal["IN_TRANSIT"] = "IN_TRANSIT"

    vessel_name: str | None = None
    vessel_imo: str | None = None
    voyage_number: str | None = None
    departure_port_code: str | None = None
    departure_port_name: str | None = None


class ShipmentOutForDeliveryPayload(_ShipmentPayloadBase):
    canonical_state: Literal["OUT_FOR_DELIVERY"] = "OUT_FOR_DELIVERY"

    destination_port_code: str | None = None
    destination_port_name: str | None = None


class ShipmentDeliveredPayload(_ShipmentPayloadBase):
    canonical_state: Literal["DELIVERED"] = "DELIVERED"

    consignee: str | None = None
    delivery_port_code: str | None = None
    delivery_order_number: str | None = None


# ---------------------------------------------------------------------------
# Invoice payloads
# ---------------------------------------------------------------------------


class InvoiceLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    amount: Decimal = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)


class InvoiceIssuedPayload(_InvoicePayloadBase):
    canonical_state: Literal["ISSUED"] = "ISSUED"

    amount: Decimal = Field(
        ge=0, description="Total invoice amount as decimal (parse '24.350,75' → 24350.75)."
    )
    issued_at: datetime
    due_at: datetime | None = None
    line_items: list[InvoiceLineItem] = Field(default_factory=list)


class InvoicePaidPayload(_InvoicePayloadBase):
    canonical_state: Literal["PAID"] = "PAID"

    amount_paid: Decimal = Field(ge=0)
    settled_at: datetime
    remitter: str | None = None
    memo: str | None = None


class InvoiceVoidedPayload(_InvoicePayloadBase):
    canonical_state: Literal["VOIDED"] = "VOIDED"

    voided_at: datetime
    void_reason: str | None = None


class InvoiceRefundedPayload(_InvoicePayloadBase):
    canonical_state: Literal["REFUNDED"] = "REFUNDED"

    refund_amount: Decimal = Field(ge=0)
    refunded_at: datetime
    refund_reason: str | None = None


# ---------------------------------------------------------------------------
# Unclassified payload
# ---------------------------------------------------------------------------


class UnclassifiedPayload(_PayloadBase):
    """Anything that is not an actionable shipment or invoice event.

    Two cases collapse into this one type — they're functionally identical
    for the platform (no entity is ever created):
      - We confidently recognize it as a non-actionable notice (port
        advisory, weather, marketing) — set `reason` to that.
      - The LLM can't fit it into any of the known shipment/invoice
        states (a novel payload pattern) — set `reason` to "novel payload,
        no template matched" or similar.

    Either way, the vendor payload is preserved verbatim in `extras` so
    nothing is lost. Persistence is controlled by
    `Settings.worker_keep_unclassified_events`:
      - True  (default) — record in the audit log; analysts can search
        later and extend the schema if a novel pattern recurs.
      - False — acknowledge the raw_event and skip further bookkeeping;
        `last_error` is set so the row is still searchable as "dropped".
    """

    entity_type: Literal["unclassified"] = "unclassified"
    canonical_state: Literal["UNCLASSIFIED"] = "UNCLASSIFIED"
    reason: str = Field(
        min_length=1,
        description="Why this is not a shipment / invoice update "
        "(e.g. 'port-congestion advisory' or 'novel payload pattern').",
    )


# ---------------------------------------------------------------------------
# Discriminated union + envelope
# ---------------------------------------------------------------------------

EventPayload = Annotated[
    ShipmentPickedUpPayload
    | ShipmentInTransitPayload
    | ShipmentOutForDeliveryPayload
    | ShipmentDeliveredPayload
    | InvoiceIssuedPayload
    | InvoicePaidPayload
    | InvoiceVoidedPayload
    | InvoiceRefundedPayload
    | UnclassifiedPayload,
    Field(discriminator="canonical_state"),
]


class NormalizedEvent(BaseModel):
    """The strict event our platform persists."""

    model_config = ConfigDict(extra="forbid")

    vendor: str = Field(
        min_length=1,
        description="Canonical vendor identifier extracted from payload (e.g. 'MAEU').",
    )
    event_at: datetime = Field(description="When the event happened, normalized to UTC.")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    payload: EventPayload

    # ---- Convenience accessors used by services -----------------------------

    @property
    def canonical_state(self) -> str:
        return self.payload.canonical_state

    @property
    def entity_type(self) -> str:
        return self.payload.entity_type

    @property
    def vendor_milestone(self) -> str | None:
        return getattr(self.payload, "vendor_milestone", None)

    @property
    def external_ref(self) -> str | None:
        p = self.payload
        if isinstance(p, _ShipmentPayloadBase):
            return p.transport_doc_number
        if isinstance(p, _InvoicePayloadBase):
            return p.document_reference
        return None
