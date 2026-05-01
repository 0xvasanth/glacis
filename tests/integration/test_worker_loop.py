"""Worker loop tests: claim semantics, concurrency, reaper, retry."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.models import RawEvent, Shipment
from app.utils.hashing import canonical_hash
from app.workers.ingest_worker import claim_one, process_one, reap_stale

from ._event_factories import shipment_event, unclassified
from ._fakes import ScriptedClassifier

pytestmark = pytest.mark.asyncio


async def _seed_pending(payload: dict) -> RawEvent:
    async with session_scope() as session:
        re = RawEvent(payload=payload, vendor_hint="v", hash_exact=canonical_hash(payload))
        session.add(re)
        await session.flush()
        await session.refresh(re)
        return re


async def test_claim_one_picks_pending_and_marks_processing():
    raw = await _seed_pending({"x": 1})
    async with session_scope() as session:
        claimed = await claim_one(session, max_attempts=5)
        assert claimed is not None
        assert claimed.id == raw.id
        assert claimed.status == "processing"
        assert claimed.attempts == 1


async def test_claim_one_returns_none_when_no_pending():
    async with session_scope() as session:
        assert await claim_one(session, max_attempts=5) is None


async def test_claim_one_skips_rows_at_max_attempts():
    raw = await _seed_pending({"x": 1})
    async with session_scope() as session:
        re = await session.get(RawEvent, raw.id)
        assert re is not None
        re.attempts = 5
    async with session_scope() as session:
        assert await claim_one(session, max_attempts=5) is None


async def test_two_concurrent_claims_get_different_rows_or_none():
    """SKIP LOCKED proof: with one row, only one claimer wins."""
    await _seed_pending({"x": 1})

    async def claim() -> RawEvent | None:
        async with session_scope() as session:
            return await claim_one(session, max_attempts=5)

    a, b = await asyncio.gather(claim(), claim())
    winners = [x for x in (a, b) if x is not None]
    assert len(winners) == 1


async def test_process_one_calls_classifier_and_persists_shipment():
    raw = await _seed_pending({"event_msg_id": "X"})
    classifier = ScriptedClassifier(
        responses=[
            shipment_event(
                "PICKED_UP",
                datetime(2026, 4, 19, 11, 15, tzinfo=UTC),
                transport_doc_number="REF1",
            )
        ]
    )

    settings = get_settings()
    did = await process_one(classifier, settings)
    assert did is True
    assert classifier.calls == [{"event_msg_id": "X"}]

    async with session_scope() as session:
        re = await session.get(RawEvent, raw.id)
        assert re is not None and re.status == "processed"
        ships = (await session.execute(select(Shipment))).scalars().all()
        assert len(ships) == 1
        assert ships[0].current_state == "PICKED_UP"


async def test_classifier_failure_marks_pending_with_error():
    raw = await _seed_pending({"x": 1})

    class BoomClassifier:
        async def classify(self, payload):  # type: ignore[no-untyped-def]
            raise RuntimeError("LLM exploded")

    settings = get_settings()
    did = await process_one(BoomClassifier(), settings)  # type: ignore[arg-type]
    assert did is True

    async with session_scope() as session:
        re = await session.get(RawEvent, raw.id)
        assert re is not None
        assert re.status == "pending"
        assert re.attempts == 1
        assert "LLM exploded" in (re.last_error or "")


async def test_reaper_recovers_stuck_processing_rows():
    raw = await _seed_pending({"x": 1})
    # Manually set the row to stuck-processing with an old lock.
    async with session_scope() as session:
        await session.execute(
            text("UPDATE raw_events SET status='processing', locked_at = :t WHERE id = :id"),
            {"t": datetime.now(UTC) - timedelta(minutes=10), "id": raw.id},
        )

    settings = get_settings()
    settings.worker_stale_lock_minutes = 5
    recovered = await reap_stale(settings)
    assert recovered == 1

    async with session_scope() as session:
        re = await session.get(RawEvent, raw.id)
        assert re is not None and re.status == "pending" and re.locked_at is None


async def test_reaper_does_not_touch_recent_locks():
    raw = await _seed_pending({"x": 1})
    async with session_scope() as session:
        await session.execute(
            text("UPDATE raw_events SET status='processing', locked_at = now() WHERE id = :id"),
            {"id": raw.id},
        )

    settings = get_settings()
    settings.worker_stale_lock_minutes = 5
    recovered = await reap_stale(settings)
    assert recovered == 0


# ----------------------------------------------------------------------------
# Worker visibility of duplicate / failed / terminal-state rows
# ----------------------------------------------------------------------------


async def test_worker_does_NOT_claim_rows_marked_duplicate():
    """Rows with status='duplicate' must be invisible to the claim queue,
    so duplicates never trigger an LLM call."""
    primary = await _seed_pending({"event_msg_id": "P"})
    # Create a duplicate row pointing at primary.
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

    # Claim should pick up exactly one row (the primary), and it must NOT be the dup.
    async with session_scope() as session:
        claimed = await claim_one(session, max_attempts=5)
        assert claimed is not None
        assert claimed.id == primary.id
        assert claimed.id != dup_id
    # And there is nothing left to claim.
    async with session_scope() as session:
        assert await claim_one(session, max_attempts=5) is None


async def test_process_one_with_unclassified_creates_no_entity():
    """When the LLM returns an Unclassified payload, the row is marked
    processed but no shipment/invoice is created."""
    raw = await _seed_pending({"some": "advisory"})
    classifier = ScriptedClassifier(responses=[unclassified(datetime(2026, 4, 26, tzinfo=UTC))])
    settings = get_settings()
    did = await process_one(classifier, settings)
    assert did is True

    async with session_scope() as session:
        re = await session.get(RawEvent, raw.id)
        assert re is not None and re.status == "processed"
        assert (await session.execute(select(Shipment))).scalars().all() == []


async def test_process_one_with_classifier_validation_error_marks_pending():
    """If the classifier raises (e.g. Pydantic ValidationError because the LLM
    omitted a required field for the chosen state), the worker resets the row
    to pending with the error message attached so an operator can triage."""
    raw = await _seed_pending({"event_msg_id": "BAD"})

    class BoomClassifier:
        async def classify(self, payload):  # type: ignore[no-untyped-def]
            raise ValueError("missing field 'transport_doc_number' for state DELIVERED")

    settings = get_settings()
    did = await process_one(BoomClassifier(), settings)  # type: ignore[arg-type]
    assert did is True

    async with session_scope() as session:
        re = await session.get(RawEvent, raw.id)
        assert re is not None
        assert re.status == "pending"
        assert "transport_doc_number" in (re.last_error or "")


async def test_max_attempts_parks_row_in_dlq():
    """After max_attempts retries, the row stays at status='pending' with
    attempts==max but is no longer claimed (effectively a DLQ)."""
    raw = await _seed_pending({"x": 1})
    async with session_scope() as session:
        re = await session.get(RawEvent, raw.id)
        assert re is not None
        re.attempts = 5  # already at max

    # Even with one pending row in the table, claim returns None because of WHERE attempts < max.
    async with session_scope() as session:
        assert await claim_one(session, max_attempts=5) is None
    # The row is still there for diagnostics.
    async with session_scope() as session:
        re = await session.get(RawEvent, raw.id)
        assert re is not None and re.status == "pending" and re.attempts == 5


async def test_two_events_same_entity_via_worker_link_to_one_shipment():
    """End-to-end: two raw events for the same shipment go through the worker
    (with a scripted classifier) and converge on one entity."""
    raw1 = await _seed_pending({"event_msg_id": "E1"})
    raw2 = await _seed_pending({"event_msg_id": "E2"})

    t1 = datetime(2026, 4, 19, 11, 0, tzinfo=UTC)
    t2 = datetime(2026, 4, 21, 14, 47, tzinfo=UTC)

    classifier = ScriptedClassifier(
        responses=[
            shipment_event("PICKED_UP", t1, transport_doc_number="MAEU-LINK"),
            shipment_event("IN_TRANSIT", t2, transport_doc_number="MAEU-LINK"),
        ]
    )

    settings = get_settings()
    assert await process_one(classifier, settings) is True
    assert await process_one(classifier, settings) is True
    assert await process_one(classifier, settings) is False  # nothing left

    async with session_scope() as session:
        ships = (await session.execute(select(Shipment))).scalars().all()
        assert len(ships) == 1
        assert ships[0].external_ref == "MAEU-LINK"
        assert ships[0].current_state == "IN_TRANSIT"
        for raw in (raw1, raw2):
            re = await session.get(RawEvent, raw.id)
            assert re is not None and re.status == "processed"
