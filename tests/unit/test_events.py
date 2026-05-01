"""Unit tests for the typed event schema (envelope + discriminated payload union).

Covers:
  - Each payload variant accepts its required fields and rejects when they're missing.
  - The discriminator (`canonical_state`) routes to the correct payload class
    when constructing a `NormalizedEvent` from raw dict input.
  - Required-field violations surface as Pydantic ValidationError listing the
    exact field name (so the worker's DLQ message is operator-actionable).
  - The `extras` bag captures arbitrary vendor-specific fields losslessly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.events import (
    InvoiceIssuedPayload,
    InvoiceLineItem,
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


def _envelope(payload, **overrides):
    return NormalizedEvent(
        vendor=overrides.get("vendor", "MAEU"),
        event_at=overrides.get("event_at", datetime(2026, 4, 21, 14, 47, tzinfo=UTC)),
        confidence=overrides.get("confidence", 1.0),
        payload=payload,
    )


# ---- Mandatory-field invariants ---------------------------------------------


def test_shipment_picked_up_requires_transport_doc_number():
    with pytest.raises(ValidationError) as exc:
        ShipmentPickedUpPayload(vendor_milestone="received at origin")  # type: ignore[call-arg]
    assert "transport_doc_number" in str(exc.value)


def test_shipment_in_transit_constructs_with_only_required_fields():
    p = ShipmentInTransitPayload(
        vendor_milestone="Loaded onboard and sailed",
        transport_doc_number="MAEU240498712",
    )
    assert p.canonical_state == "IN_TRANSIT"
    assert p.entity_type == "shipment"


def test_envelope_external_ref_returns_transport_doc_number_for_shipments():
    e = _envelope(
        ShipmentDeliveredPayload(
            vendor_milestone="Cargo released to consignee",
            transport_doc_number="ONEYMBLHKG260499",
        ),
        vendor="ONEY",
    )
    assert e.external_ref == "ONEYMBLHKG260499"
    assert e.canonical_state == "DELIVERED"


def test_envelope_external_ref_returns_document_reference_for_invoices():
    e = _envelope(
        InvoiceIssuedPayload(
            vendor_milestone="raised",
            document_reference="GFP-INV-1",
            currency="EUR",
            amount=Decimal("100"),
            issued_at=datetime(2026, 4, 15, tzinfo=UTC),
        ),
        vendor="globalfreightpay",
    )
    assert e.external_ref == "GFP-INV-1"
    assert e.entity_type == "invoice"


def test_envelope_external_ref_is_none_for_unclassified():
    e = _envelope(
        UnclassifiedPayload(reason="port-congestion advisory, not actionable"),
        vendor="marine-traffic-advisory",
    )
    assert e.external_ref is None
    assert e.entity_type == "unclassified"


def test_invoice_issued_requires_amount_currency_and_issued_at():
    base = {
        "vendor_milestone": "freight invoice raised",
        "document_reference": "GFP-INV-1",
    }
    with pytest.raises(ValidationError) as exc:
        InvoiceIssuedPayload(
            **base, amount=Decimal("100"), issued_at=datetime(2026, 4, 15, tzinfo=UTC)
        )  # type: ignore[arg-type]
    assert "currency" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        InvoiceIssuedPayload(**base, currency="EUR", issued_at=datetime(2026, 4, 15, tzinfo=UTC))  # type: ignore[arg-type]
    assert "amount" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        InvoiceIssuedPayload(**base, currency="EUR", amount=Decimal("100"))  # type: ignore[arg-type]
    assert "issued_at" in str(exc.value)


def test_invoice_paid_requires_amount_paid_currency_and_settled_at():
    with pytest.raises(ValidationError) as exc:
        InvoicePaidPayload(  # type: ignore[call-arg]
            vendor_milestone="settled",
            document_reference="INV-1",
            currency="EUR",
        )
    msg = str(exc.value)
    assert "amount_paid" in msg
    assert "settled_at" in msg


def test_invoice_voided_constructs_with_only_required_fields():
    p = InvoiceVoidedPayload(
        vendor_milestone="cancelled",
        document_reference="INV-1",
        currency="EUR",
        voided_at=datetime(2026, 4, 17, tzinfo=UTC),
    )
    assert p.canonical_state == "VOIDED"


def test_invoice_refunded_constructs_with_required_fields():
    p = InvoiceRefundedPayload(
        vendor_milestone="refund issued",
        document_reference="INV-1",
        currency="EUR",
        refund_amount=Decimal("100"),
        refunded_at=datetime(2026, 4, 25, tzinfo=UTC),
    )
    assert p.canonical_state == "REFUNDED"


def test_unclassified_requires_only_reason():
    with pytest.raises(ValidationError) as exc:
        UnclassifiedPayload()  # type: ignore[call-arg]
    assert "reason" in str(exc.value)


def test_currency_must_be_three_chars():
    with pytest.raises(ValidationError):
        InvoiceIssuedPayload(
            vendor_milestone="raised",
            document_reference="INV-1",
            currency="EU",  # too short
            amount=Decimal("100"),
            issued_at=datetime(2026, 4, 15, tzinfo=UTC),
        )


def test_invoice_amount_must_be_non_negative():
    with pytest.raises(ValidationError):
        InvoiceIssuedPayload(
            vendor_milestone="raised",
            document_reference="INV-1",
            currency="EUR",
            amount=Decimal("-1"),
            issued_at=datetime(2026, 4, 15, tzinfo=UTC),
        )


def test_extra_fields_on_typed_payload_are_rejected():
    """`extra=forbid` catches the LLM smuggling unexpected fields through.
    Genuine vendor-specific data should go in `extras`, not as new top-level
    keys."""
    with pytest.raises(ValidationError):
        ShipmentPickedUpPayload(
            vendor_milestone="received",
            transport_doc_number="MAEU1",
            unknown_field="oops",  # type: ignore[call-arg]
        )


def test_envelope_requires_event_at_and_vendor():
    with pytest.raises(ValidationError) as exc:
        NormalizedEvent(  # type: ignore[call-arg]
            payload=UnclassifiedPayload(reason="x"),
        )
    msg = str(exc.value)
    assert "vendor" in msg
    assert "event_at" in msg


# ---- extras bag captures vendor-specific data losslessly -------------------


def test_extras_captures_arbitrary_vendor_fields_on_shipments():
    p = ShipmentPickedUpPayload(
        vendor_milestone="received",
        transport_doc_number="MAEU1",
        extras={"shipper_account_id": "ACC-9921", "priority": "GOLD"},
    )
    assert p.extras["shipper_account_id"] == "ACC-9921"
    assert p.extras["priority"] == "GOLD"


def test_extras_is_primary_storage_for_unclassified_payloads():
    """For UNCLASSIFIED, advisory-specific fields (severity, body, …) live in extras."""
    p = UnclassifiedPayload(
        reason="port-congestion advisory",
        extras={
            "advisory_id": "MTA-1",
            "severity": "AMBER",
            "subject": "Antwerp congestion",
            "body": "Vessel waiting times have increased to 4-6 days...",
            "affected_services": ["AE7", "FAL3"],
            "expires_at": "2026-05-03T00:00:00Z",
        },
    )
    assert p.extras["severity"] == "AMBER"
    assert p.extras["affected_services"] == ["AE7", "FAL3"]


def test_extras_defaults_to_empty_dict():
    p = ShipmentInTransitPayload(
        vendor_milestone="sailed",
        transport_doc_number="MAEU1",
    )
    assert p.extras == {}


def test_invoice_issued_captures_line_items():
    p = InvoiceIssuedPayload(
        vendor_milestone="raised",
        document_reference="INV-1",
        currency="EUR",
        amount=Decimal("24350.75"),
        issued_at=datetime(2026, 4, 15, tzinfo=UTC),
        line_items=[
            InvoiceLineItem(description="Ocean freight", amount=Decimal("21000"), currency="EUR"),
            InvoiceLineItem(description="THC", amount=Decimal("1850.75"), currency="EUR"),
        ],
    )
    assert len(p.line_items) == 2


# ---- Discriminated dispatch from raw dict -----------------------------------


def test_discriminator_routes_in_transit_payload():
    e = NormalizedEvent.model_validate(
        {
            "vendor": "MAEU",
            "event_at": "2026-04-21T14:47:00Z",
            "payload": {
                "canonical_state": "IN_TRANSIT",
                "vendor_milestone": "Loaded onboard and sailed",
                "transport_doc_number": "MAEU240498712",
                "vessel_name": "MAERSK GUATEMALA",
            },
        }
    )
    assert isinstance(e.payload, ShipmentInTransitPayload)
    assert e.payload.vessel_name == "MAERSK GUATEMALA"


def test_discriminator_routes_paid_payload():
    e = NormalizedEvent.model_validate(
        {
            "vendor": "globalfreightpay",
            "event_at": "2026-04-22T16:47:11Z",
            "payload": {
                "canonical_state": "PAID",
                "vendor_milestone": "settled in full",
                "document_reference": "GFP-INV-1",
                "currency": "EUR",
                "amount_paid": "24350.75",
                "settled_at": "2026-04-22T16:47:11Z",
                "remitter": "ACME Logistics GmbH",
            },
        }
    )
    assert isinstance(e.payload, InvoicePaidPayload)
    assert e.payload.amount_paid == Decimal("24350.75")


def test_discriminator_routes_unclassified_payload_with_extras():
    e = NormalizedEvent.model_validate(
        {
            "vendor": "marine-traffic-advisory",
            "event_at": "2026-04-26T06:00:00Z",
            "payload": {
                "canonical_state": "UNCLASSIFIED",
                "reason": "port-congestion advisory",
                "extras": {"severity": "AMBER", "advisory_id": "MTA-1"},
            },
        }
    )
    assert isinstance(e.payload, UnclassifiedPayload)
    assert e.payload.extras["severity"] == "AMBER"


def test_discriminator_dispatch_raises_when_required_field_missing():
    """LLM picks PAID but forgets settled_at — must surface that clearly."""
    with pytest.raises(ValidationError) as exc:
        NormalizedEvent.model_validate(
            {
                "vendor": "x",
                "event_at": "2026-04-22T00:00:00Z",
                "payload": {
                    "canonical_state": "PAID",
                    "vendor_milestone": "settled",
                    "document_reference": "INV-1",
                    "currency": "EUR",
                    "amount_paid": "100",
                    # settled_at missing
                },
            }
        )
    assert "settled_at" in str(exc.value)


def test_discriminator_dispatch_raises_when_shipment_missing_transport_doc():
    with pytest.raises(ValidationError) as exc:
        NormalizedEvent.model_validate(
            {
                "vendor": "ONEY",
                "event_at": "2026-04-28T02:42:00Z",
                "payload": {
                    "canonical_state": "DELIVERED",
                    "vendor_milestone": "delivered",
                    # transport_doc_number missing
                },
            }
        )
    assert "transport_doc_number" in str(exc.value)


def test_out_for_delivery_constructs_with_only_required_fields():
    p = ShipmentOutForDeliveryPayload(
        vendor_milestone="on truck",
        transport_doc_number="UPS-1",
    )
    assert p.canonical_state == "OUT_FOR_DELIVERY"
