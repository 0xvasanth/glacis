"""dedup-by-hash: drop dedupe_key, add hash_exact + duplicate_of_id

The API computes a byte-canonical SHA-256 of the payload and looks for an
existing parent of the same vendor. If found, the new row is written with
status='duplicate' and duplicate_of_id pointing at the parent. If not, it
goes through the normal pending → processed lifecycle.

The 'duplicate' status keeps full audit (every payload is persisted) while
keeping duplicates out of the worker's claim queue (which selects only
status='pending').
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_raw_events_pending", table_name="raw_events")
    op.drop_constraint("uq_raw_events_dedupe_key", "raw_events", type_="unique")
    op.drop_column("raw_events", "dedupe_key")

    op.add_column("raw_events", sa.Column("hash_exact", sa.String(64), nullable=False))
    op.add_column(
        "raw_events",
        sa.Column(
            "duplicate_of_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_raw_events_pending",
        "raw_events",
        ["status", "received_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    # Used by the API to find an existing parent before deciding whether
    # this incoming row is a primary or a duplicate.
    op.create_index(
        "ix_raw_events_hash_exact_vendor",
        "raw_events",
        ["vendor_hint", "hash_exact"],
    )


def downgrade() -> None:
    op.drop_index("ix_raw_events_hash_exact_vendor", table_name="raw_events")
    op.drop_index("ix_raw_events_pending", table_name="raw_events")
    op.drop_column("raw_events", "duplicate_of_id")
    op.drop_column("raw_events", "hash_exact")
    op.add_column("raw_events", sa.Column("dedupe_key", sa.String(128), nullable=False))
    op.create_unique_constraint("uq_raw_events_dedupe_key", "raw_events", ["dedupe_key"])
    op.create_index(
        "ix_raw_events_pending",
        "raw_events",
        ["status", "received_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
