"""Service-layer + endpoint tests for the manual retry path."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api import create_app
from app.api.dependencies import get_classifier
from app.core.db import session_scope
from app.core.models import RawEvent, Shipment, ShipmentEvent
from app.services.retry import RetryNotFoundError, RetryRefusedError, retry_raw_event
from app.utils.hashing import canonical_hash

from ._event_factories import shipment_event, unclassified
from ._fakes import ScriptedClassifier

pytestmark = pytest.mark.asyncio


async def _seed_pending(payload: dict, **overrides) -> RawEvent:
    async with session_scope() as session:
        re = RawEvent(
            payload=payload,
            vendor_hint=overrides.get("vendor_hint", "v"),
            hash_exact=canonical_hash(payload),
            **{k: v for k, v in overrides.items() if k != "vendor_hint"},
        )
        session.add(re)
        await session.flush()
        await session.refresh(re)
        return re


# ---- service-layer tests ----------------------------------------------------


async def test_retry_runs_classifier_and_persists_pending_row():
    raw = await _seed_pending({"event_msg_id": "X"})
    classifier = ScriptedClassifier(
        responses=[
            shipment_event(
                "PICKED_UP",
                datetime(2026, 4, 19, tzinfo=UTC),
                transport_doc_number="REF-RETRY-1",
            )
        ]
    )

    result = await retry_raw_event(classifier, raw.id)
    assert result.status == "processed"
    assert result.attempts == 1
    assert classifier.calls == [{"event_msg_id": "X"}]

    async with session_scope() as session:
        ships = (await session.execute(select(Shipment))).scalars().all()
        assert len(ships) == 1
        assert ships[0].external_ref == "REF-RETRY-1"


async def test_retry_increments_attempts_on_existing_failures():
    """Manual retry of a row that already has prior attempts (e.g. parked at
    max_attempts) should keep counting attempts so DLQ logic still reflects
    real history."""
    raw = await _seed_pending({"event_msg_id": "Y"})
    async with session_scope() as session:
        re = await session.get(RawEvent, raw.id)
        assert re is not None
        re.attempts = 5  # already at max from prior worker tries
        re.last_error = "previous LLM timeout"

    classifier = ScriptedClassifier(
        responses=[
            shipment_event(
                "DELIVERED",
                datetime(2026, 4, 28, tzinfo=UTC),
                transport_doc_number="REF-RETRY-2",
            )
        ]
    )
    result = await retry_raw_event(classifier, raw.id)
    assert result.status == "processed"
    assert result.attempts == 6  # incremented from 5
    assert result.last_error is None  # cleared on success


async def test_retry_raises_RetryNotFound_for_unknown_id():
    with pytest.raises(RetryNotFoundError):
        await retry_raw_event(ScriptedClassifier(responses=[]), uuid.uuid4())


async def test_retry_raises_RetryRefused_for_duplicate_row():
    primary = await _seed_pending({"event_msg_id": "P"})
    async with session_scope() as session:
        dup = RawEvent(
            payload={"event_msg_id": "P"},
            vendor_hint="v",
            hash_exact=canonical_hash({"event_msg_id": "P"}),
            duplicate_of_id=primary.id,
            status="duplicate",
        )
        session.add(dup)
        await session.flush()
        dup_id = dup.id

    with pytest.raises(RetryRefusedError) as exc:
        await retry_raw_event(ScriptedClassifier(responses=[]), dup_id)
    assert exc.value.status == "duplicate"


async def test_retry_raises_RetryRefused_for_already_processed_row():
    """Once a row is processed, *_events.raw_event_id UNIQUE means a re-run
    is a no-op. Refuse loudly so the operator knows nothing happened."""
    raw = await _seed_pending({"event_msg_id": "Z"})
    classifier = ScriptedClassifier(
        responses=[
            shipment_event(
                "PICKED_UP",
                datetime(2026, 4, 19, tzinfo=UTC),
                transport_doc_number="REF-Z",
            )
        ]
    )
    await retry_raw_event(classifier, raw.id)

    with pytest.raises(RetryRefusedError) as exc:
        await retry_raw_event(ScriptedClassifier(responses=[]), raw.id)
    assert exc.value.status == "processed"


async def test_retry_resets_to_pending_with_error_on_classifier_failure():
    raw = await _seed_pending({"event_msg_id": "BOOM"})

    class BoomClassifier:
        async def classify(self, payload):  # type: ignore[no-untyped-def]
            raise RuntimeError("LLM 500")

    with pytest.raises(RuntimeError):
        await retry_raw_event(BoomClassifier(), raw.id)  # type: ignore[arg-type]

    # Row reset so the worker (or another retry) can pick it up again.
    async with session_scope() as session:
        re = await session.get(RawEvent, raw.id)
        assert re is not None
        assert re.status == "pending"
        assert "LLM 500" in (re.last_error or "")
        assert re.attempts == 1


async def test_retry_with_unclassified_marks_processed_no_entity():
    raw = await _seed_pending({"some": "advisory"})
    classifier = ScriptedClassifier(responses=[unclassified(datetime(2026, 4, 26, tzinfo=UTC))])

    result = await retry_raw_event(classifier, raw.id)
    assert result.status == "processed"

    async with session_scope() as session:
        assert (await session.execute(select(Shipment))).scalars().all() == []


# ---- endpoint tests --------------------------------------------------------


def _client_with_classifier(classifier) -> AsyncClient:
    app = create_app()
    app.dependency_overrides[get_classifier] = lambda: classifier
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_endpoint_returns_200_and_canonical_state_on_success():
    raw = await _seed_pending({"event_msg_id": "API-1"})
    classifier = ScriptedClassifier(
        responses=[
            shipment_event(
                "IN_TRANSIT",
                datetime(2026, 4, 21, tzinfo=UTC),
                transport_doc_number="REF-API-1",
            )
        ]
    )

    async with _client_with_classifier(classifier) as ac:
        r = await ac.post(f"/api/v1/raw-events/{raw.id}/retry")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "processed"
    assert body["canonical_state"] == "IN_TRANSIT"
    assert body["entity_type"] == "shipment"
    assert body["last_error"] is None


async def test_endpoint_returns_404_for_unknown_id():
    async with _client_with_classifier(ScriptedClassifier(responses=[])) as ac:
        r = await ac.post(f"/api/v1/raw-events/{uuid.uuid4()}/retry")
    assert r.status_code == 404


async def test_endpoint_returns_409_for_already_processed_row():
    raw = await _seed_pending({"event_msg_id": "API-2"})
    classifier = ScriptedClassifier(
        responses=[
            shipment_event(
                "PICKED_UP",
                datetime(2026, 4, 19, tzinfo=UTC),
                transport_doc_number="REF-API-2",
            )
        ]
    )
    async with _client_with_classifier(classifier) as ac:
        first = await ac.post(f"/api/v1/raw-events/{raw.id}/retry")
        assert first.status_code == 200
        second = await ac.post(f"/api/v1/raw-events/{raw.id}/retry")
    assert second.status_code == 409


async def test_endpoint_returns_502_on_llm_failure():
    raw = await _seed_pending({"event_msg_id": "API-3"})

    class BoomClassifier:
        async def classify(self, payload):  # type: ignore[no-untyped-def]
            raise RuntimeError("upstream LLM exploded")

    async with _client_with_classifier(BoomClassifier()) as ac:
        r = await ac.post(f"/api/v1/raw-events/{raw.id}/retry")
    assert r.status_code == 502
    # Row is reset so a follow-up worker / retry can try again.
    async with session_scope() as session:
        re = await session.get(RawEvent, raw.id)
        assert re is not None
        assert re.status == "pending"
        assert "upstream LLM exploded" in (re.last_error or "")


async def test_endpoint_records_attempts_and_creates_event_row():
    raw = await _seed_pending({"event_msg_id": "API-4"})
    classifier = ScriptedClassifier(
        responses=[
            shipment_event(
                "PICKED_UP",
                datetime(2026, 4, 19, tzinfo=UTC),
                transport_doc_number="REF-API-4",
            )
        ]
    )
    async with _client_with_classifier(classifier) as ac:
        r = await ac.post(f"/api/v1/raw-events/{raw.id}/retry")
    assert r.status_code == 200
    assert r.json()["attempts"] == 1

    async with session_scope() as session:
        events = (await session.execute(select(ShipmentEvent))).scalars().all()
        assert len(events) == 1
        assert events[0].canonical_state == "PICKED_UP"
