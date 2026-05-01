from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB}


# ---- raw events ----------------------------------------------------------------


class RawEvent(Base):
    __tablename__ = "raw_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    vendor_hint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    # Byte-canonical SHA-256 of the payload. Computed at API time and used to
    # find an existing parent when this incoming row is a byte-identical retry.
    hash_exact: Mapped[str] = mapped_column(String(64), nullable=False)

    # When this row is a duplicate, points at the canonical parent. NULL otherwise.
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_events.id", ondelete="SET NULL"), nullable=True
    )

    # 'pending' | 'processing' | 'processed' | 'failed' | 'duplicate'
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(String(2048))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "ix_raw_events_pending",
            "status",
            "received_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "uq_raw_events_primary_per_hash",
            "vendor_hint",
            "hash_exact",
            unique=True,
            postgresql_where=text("duplicate_of_id IS NULL"),
        ),
    )


# ---- shipments -----------------------------------------------------------------


class Shipment(Base):
    __tablename__ = "shipments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    vendor: Mapped[str] = mapped_column(String(64), nullable=False)
    external_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    current_state: Mapped[str | None] = mapped_column(String(32))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    events: Mapped[list[ShipmentEvent]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (UniqueConstraint("vendor", "external_ref", name="uq_shipments_vendor_ref"),)


class ShipmentEvent(Base):
    __tablename__ = "shipment_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False
    )
    raw_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_events.id"), nullable=False, unique=True
    )
    canonical_state: Mapped[str] = mapped_column(String(32), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    vendor_milestone: Mapped[str | None] = mapped_column(String(512))
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    shipment: Mapped[Shipment] = relationship(back_populates="events")

    __table_args__ = (Index("ix_shipment_events_entity_event_at", "entity_id", "event_at"),)


# ---- invoices ------------------------------------------------------------------


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    vendor: Mapped[str] = mapped_column(String(64), nullable=False)
    external_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    current_state: Mapped[str | None] = mapped_column(String(32))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    events: Mapped[list[InvoiceEvent]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (UniqueConstraint("vendor", "external_ref", name="uq_invoices_vendor_ref"),)


class InvoiceEvent(Base):
    __tablename__ = "invoice_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False
    )
    raw_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_events.id"), nullable=False, unique=True
    )
    canonical_state: Mapped[str] = mapped_column(String(32), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    vendor_milestone: Mapped[str | None] = mapped_column(String(512))
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    invoice: Mapped[Invoice] = relationship(back_populates="events")

    __table_args__ = (Index("ix_invoice_events_entity_event_at", "entity_id", "event_at"),)
