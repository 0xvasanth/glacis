"""initial schema

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    op.create_table(
        "raw_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("dedupe_key", sa.String(128), nullable=False),
        sa.Column("vendor_hint", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.String(2048), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("dedupe_key", name="uq_raw_events_dedupe_key"),
    )
    op.create_index(
        "ix_raw_events_pending",
        "raw_events",
        ["status", "received_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "shipments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("vendor", sa.String(64), nullable=False),
        sa.Column("external_ref", sa.String(128), nullable=False),
        sa.Column("current_state", sa.String(32), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attributes", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("vendor", "external_ref", name="uq_shipments_vendor_ref"),
    )

    op.create_table(
        "shipment_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("raw_event_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("raw_events.id"), nullable=False),
        sa.Column("canonical_state", sa.String(32), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("vendor_milestone", sa.String(512), nullable=True),
        sa.Column("attributes", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("raw_event_id", name="uq_shipment_events_raw_event_id"),
    )
    op.create_index(
        "ix_shipment_events_entity_event_at",
        "shipment_events",
        ["entity_id", "event_at"],
    )

    op.create_table(
        "invoices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("vendor", sa.String(64), nullable=False),
        sa.Column("external_ref", sa.String(128), nullable=False),
        sa.Column("current_state", sa.String(32), nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attributes", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("vendor", "external_ref", name="uq_invoices_vendor_ref"),
    )

    op.create_table(
        "invoice_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("raw_event_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("raw_events.id"), nullable=False),
        sa.Column("canonical_state", sa.String(32), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("vendor_milestone", sa.String(512), nullable=True),
        sa.Column("attributes", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("raw_event_id", name="uq_invoice_events_raw_event_id"),
    )
    op.create_index(
        "ix_invoice_events_entity_event_at",
        "invoice_events",
        ["entity_id", "event_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_invoice_events_entity_event_at", table_name="invoice_events")
    op.drop_table("invoice_events")
    op.drop_table("invoices")
    op.drop_index("ix_shipment_events_entity_event_at", table_name="shipment_events")
    op.drop_table("shipment_events")
    op.drop_table("shipments")
    op.drop_index("ix_raw_events_pending", table_name="raw_events")
    op.drop_table("raw_events")
