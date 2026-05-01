"""Regression: concurrent identical webhook POSTs MUST produce exactly one
PRIMARY raw_event row.

Without the partial unique index, two parallel SELECTs both miss each
other before either INSERT commits, and both rows become primaries — the
worker then pays for the LLM twice on the same content. The
`uq_raw_events_primary_per_hash` partial UNIQUE index combined with
INSERT ... ON CONFLICT DO NOTHING in `app.services.ingestion` is what
forces the loser into a duplicate row pointing at the winner.

These tests run the race at two levels:
  - service layer: N concurrent calls to `insert_raw_event` with separate sessions
  - HTTP layer:    N concurrent POSTs through the FastAPI app
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api import create_app
from app.core.db import session_scope
from app.core.models import RawEvent
from app.services.ingestion import insert_raw_event

pytestmark = pytest.mark.asyncio


# ---- service-layer race ----------------------------------------------------


async def _insert_one(payload: dict[str, Any]) -> tuple[bool, Any]:
    async with session_scope() as session:
        result = await insert_raw_event(session, vendor_hint="maersk", payload=payload)
    return (result.duplicate, result.raw_event_id)


async def test_two_concurrent_inserts_yield_exactly_one_primary():
    payload = {"event_msg_id": "RACE-1", "milestone": "Loaded"}
    a, b = await asyncio.gather(_insert_one(payload), _insert_one(payload))

    primaries = [r for r in (a, b) if not r[0]]
    duplicates = [r for r in (a, b) if r[0]]
    assert len(primaries) == 1, f"expected 1 primary, got {len(primaries)}"
    assert len(duplicates) == 1

    async with session_scope() as session:
        rows = (
            (await session.execute(select(RawEvent).order_by(RawEvent.received_at))).scalars().all()
        )
    assert len(rows) == 2
    primary_rows = [r for r in rows if r.duplicate_of_id is None]
    duplicate_rows = [r for r in rows if r.duplicate_of_id is not None]
    assert len(primary_rows) == 1
    assert len(duplicate_rows) == 1
    assert duplicate_rows[0].duplicate_of_id == primary_rows[0].id
    assert duplicate_rows[0].status == "duplicate"
    assert primary_rows[0].status == "pending"


async def test_eight_concurrent_inserts_all_chain_to_one_primary():
    payload = {"event_msg_id": "RACE-N", "milestone": "Loaded"}
    results = await asyncio.gather(*[_insert_one(payload) for _ in range(8)])
    primaries = [r for r in results if not r[0]]
    duplicates = [r for r in results if r[0]]
    assert len(primaries) == 1
    assert len(duplicates) == 7

    async with session_scope() as session:
        rows = (await session.execute(select(RawEvent))).scalars().all()
    primary_id = next(r.id for r in rows if r.duplicate_of_id is None)
    for r in rows:
        if r.duplicate_of_id is not None:
            assert r.duplicate_of_id == primary_id


async def test_different_payloads_under_concurrency_remain_independent():
    """Concurrency should NOT collapse different payloads into one entity."""
    p1 = {"event_msg_id": "DIFF-1"}
    p2 = {"event_msg_id": "DIFF-2"}
    a, b = await asyncio.gather(_insert_one(p1), _insert_one(p2))
    assert a[0] is False and b[0] is False  # both primaries

    async with session_scope() as session:
        rows = (await session.execute(select(RawEvent))).scalars().all()
    assert len(rows) == 2
    assert all(r.duplicate_of_id is None for r in rows)


# ---- HTTP-layer race -------------------------------------------------------


async def _post_one(client: AsyncClient, payload: dict[str, Any]):
    return await client.post("/api/v1/webhooks/maersk", json=payload)


async def test_concurrent_HTTP_posts_yield_exactly_one_primary():
    payload = {"event_msg_id": "HTTP-RACE-1", "milestone": "Loaded"}
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        results = await asyncio.gather(*[_post_one(ac, payload) for _ in range(5)])

    for r in results:
        assert r.status_code == 202

    bodies = [r.json() for r in results]
    primaries = [b for b in bodies if not b["duplicate"]]
    duplicates = [b for b in bodies if b["duplicate"]]
    assert len(primaries) == 1, f"expected 1 primary, got {len(primaries)}: {primaries}"
    assert len(duplicates) == 4
    parent_id = primaries[0]["raw_event_id"]
    for d in duplicates:
        assert d["duplicate_of"] == parent_id

    async with session_scope() as session:
        rows = (await session.execute(select(RawEvent))).scalars().all()
    assert len([r for r in rows if r.duplicate_of_id is None]) == 1
