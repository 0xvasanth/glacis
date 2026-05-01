"""Close the dedup TOCTOU race with a partial unique index.

Adds a partial UNIQUE index `uq_raw_events_primary_per_hash` that
guarantees at most one PRIMARY raw_event (i.e. one with
`duplicate_of_id IS NULL`) per `(vendor_hint, hash_exact)`. Duplicate rows
(those pointing at a parent) are unaffected — they may share the hash.

Without this constraint, two concurrent identical POSTs both pass the
SELECT-then-INSERT check before either commits, so both become primaries
and the worker pays for the LLM twice on the same content. The atomic
backstop turns the second concurrent INSERT into an ON CONFLICT, which
the ingestion service catches and writes as a duplicate.

Also drops the prior non-unique helper index that's now redundant — the
unique index covers the same lookup pattern.

Revision ID: 0003
Revises: 0002

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_raw_events_hash_exact_vendor", table_name="raw_events")
    op.create_index(
        "uq_raw_events_primary_per_hash",
        "raw_events",
        ["vendor_hint", "hash_exact"],
        unique=True,
        postgresql_where=sa.text("duplicate_of_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_raw_events_primary_per_hash", table_name="raw_events")
    op.create_index(
        "ix_raw_events_hash_exact_vendor",
        "raw_events",
        ["vendor_hint", "hash_exact"],
    )
