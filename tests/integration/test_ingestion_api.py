from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api import create_app
from app.core.db import session_scope
from app.core.models import RawEvent

pytestmark = pytest.mark.asyncio


async def _client() -> AsyncClient:
    app = create_app()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_post_webhook_returns_202_and_persists_raw_event():
    payload = {"event_msg_id": "MAEU-EVT-1", "milestone": "Loaded"}
    async with await _client() as ac:
        r = await ac.post("/api/v1/webhooks/maersk", json=payload)
    assert r.status_code == 202
    body = r.json()
    assert body["duplicate"] is False
    assert body["duplicate_of"] is None
    assert body["raw_event_id"]

    async with session_scope() as session:
        rows = (await session.execute(select(RawEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].vendor_hint == "maersk"
    assert rows[0].status == "pending"
    assert rows[0].duplicate_of_id is None
    assert len(rows[0].hash_exact) == 64


async def test_byte_identical_repost_returns_duplicate_pointing_at_parent():
    payload = {"event_msg_id": "DUP-1"}
    async with await _client() as ac:
        r1 = await ac.post("/api/v1/webhooks/maersk", json=payload)
        r2 = await ac.post("/api/v1/webhooks/maersk", json=payload)

    body1, body2 = r1.json(), r2.json()
    assert r1.status_code == 202 and body1["duplicate"] is False
    assert r2.status_code == 202 and body2["duplicate"] is True
    # Duplicate row gets a NEW id but points at the parent.
    assert body2["raw_event_id"] != body1["raw_event_id"]
    assert body2["duplicate_of"] == body1["raw_event_id"]

    async with session_scope() as session:
        rows = (
            (await session.execute(select(RawEvent).order_by(RawEvent.received_at))).scalars().all()
        )
    assert len(rows) == 2
    assert rows[0].status == "pending" and rows[0].duplicate_of_id is None
    assert rows[1].status == "duplicate" and rows[1].duplicate_of_id == rows[0].id


async def test_three_byte_identical_posts_all_chain_to_first():
    payload = {"event_msg_id": "T1"}
    async with await _client() as ac:
        r1 = await ac.post("/api/v1/webhooks/maersk", json=payload)
        r2 = await ac.post("/api/v1/webhooks/maersk", json=payload)
        r3 = await ac.post("/api/v1/webhooks/maersk", json=payload)

    parent_id = r1.json()["raw_event_id"]
    assert r2.json()["duplicate_of"] == parent_id
    assert r3.json()["duplicate_of"] == parent_id  # no chain-of-chain; both point at root.


async def test_same_payload_different_vendor_is_not_duplicate():
    payload = {"event_id": "X-1"}
    async with await _client() as ac:
        a = await ac.post("/api/v1/webhooks/maersk", json=payload)
        b = await ac.post("/api/v1/webhooks/oney", json=payload)
    assert a.json()["duplicate"] is False
    assert b.json()["duplicate"] is False  # different vendor namespace.


async def test_different_payloads_same_vendor_are_not_duplicate():
    """Two genuinely-different events for the same vendor must both be processed."""
    issued = {
        "doc_ref": "GFP-INV-1",
        "transaction": {"kind": "freight invoice raised"},
    }
    paid = {
        "doc_ref": "GFP-INV-1",
        "transaction": {"kind": "settled in full"},
    }
    async with await _client() as ac:
        a = await ac.post("/api/v1/webhooks/gfp", json=issued)
        b = await ac.post("/api/v1/webhooks/gfp", json=paid)
    assert a.json()["duplicate"] is False
    assert b.json()["duplicate"] is False
    assert a.json()["raw_event_id"] != b.json()["raw_event_id"]


async def test_post_webhook_rejects_invalid_json():
    async with await _client() as ac:
        r = await ac.post(
            "/api/v1/webhooks/maersk",
            content=b"not-json",
            headers={"content-type": "application/json"},
        )
    assert r.status_code == 400


async def test_post_webhook_rejects_non_object_root():
    async with await _client() as ac:
        r = await ac.post("/api/v1/webhooks/maersk", json=[1, 2, 3])
    assert r.status_code == 400


async def test_post_webhook_rejects_invalid_vendor_path():
    async with await _client() as ac:
        r = await ac.post("/api/v1/webhooks/has space", json={"a": 1})
    assert r.status_code == 422


async def test_healthz_ok():
    async with await _client() as ac:
        r = await ac.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
