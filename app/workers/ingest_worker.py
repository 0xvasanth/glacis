"""Worker process: claim raw events, classify via LLM, persist normalized."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import dispose_engine, session_scope
from app.core.models import RawEvent
from app.llm import build_llm
from app.llm.classifier import Classifier, SupportsClassify
from app.services.normalization import persist_normalized

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------


async def claim_one(session: AsyncSession, *, max_attempts: int) -> RawEvent | None:
    """Atomic claim. Returns the locked RawEvent or None when nothing is pending.

    Note: `attempts` is NOT incremented here. A claim that ends in a process
    crash should not consume retry budget — the reaper resets the row and the
    next claim is "free". `attempts` is incremented only by `mark_failed`,
    which fires on real classify / persist failures (LLM timeout, validation
    error, etc.).
    """
    stmt = text(
        """
        UPDATE raw_events
        SET status = 'processing',
            locked_at = now()
        WHERE id = (
            SELECT id FROM raw_events
            WHERE status = 'pending'
            ORDER BY received_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id
        """
    )
    result = await session.execute(stmt, {"max_attempts": max_attempts})
    claimed_id = result.scalar_one_or_none()
    if claimed_id is None:
        return None
    return await session.get(RawEvent, claimed_id)


async def mark_failed(raw_event_id: uuid.UUID, error: str, *, max_attempts: int) -> None:
    """Record a real classify/persist failure.

    Increments `attempts`. If the row reaches `max_attempts`, it goes to the
    terminal `failed` state — no longer claimable, surfaces in operator
    queries (`SELECT * FROM raw_events WHERE status='failed'`). Otherwise
    the row goes back to `pending` for another worker pass.
    """
    async with session_scope() as session:
        row = await session.get(RawEvent, raw_event_id)
        if row is None:
            return
        row.attempts = (row.attempts or 0) + 1
        row.last_error = error[:2000]
        row.locked_at = None
        row.status = "failed" if row.attempts >= max_attempts else "pending"


async def reap_stale(settings: Settings) -> int:
    """Reset stuck 'processing' rows whose lock is older than the threshold.

    Worker crashes between `claim_one` (which commits) and `persist_normalized`
    leave rows in 'processing' with no holder. The reaper recovers them.
    """
    threshold = datetime.now(tz=UTC) - timedelta(minutes=settings.worker_stale_lock_minutes)
    async with session_scope() as session:
        result = await session.execute(
            text(
                """
                UPDATE raw_events
                SET status = 'pending', locked_at = NULL
                WHERE status = 'processing' AND locked_at < :threshold
                RETURNING id
                """
            ),
            {"threshold": threshold},
        )
        recovered = result.scalars().all()
    if recovered:
        logger.warning("worker.reaper.recovered", extra={"count": len(recovered)})
    return len(recovered)


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------


async def process_one(classifier: SupportsClassify, settings: Settings) -> bool:
    """Try to claim and process one event. Returns True if work was done."""
    async with session_scope() as session:
        raw = await claim_one(session, max_attempts=settings.worker_max_attempts)
        if raw is None:
            return False
        # Snapshot fields we need; the row is now safely claimed by us.
        raw_id = raw.id
        payload = raw.payload

    try:
        result = await classifier.classify(payload)
    except Exception as exc:
        logger.exception("worker.classify_failed", extra={"raw_event_id": str(raw_id)})
        await mark_failed(raw_id, f"classify: {exc!r}", max_attempts=settings.worker_max_attempts)
        return True

    try:
        async with session_scope() as session:
            row = await session.get(RawEvent, raw_id)
            if row is None:
                logger.error("worker.raw_event_missing", extra={"raw_event_id": str(raw_id)})
                return True
            await persist_normalized(session, row, result)
    except Exception as exc:
        logger.exception("worker.persist_failed", extra={"raw_event_id": str(raw_id)})
        await mark_failed(raw_id, f"persist: {exc!r}", max_attempts=settings.worker_max_attempts)
        return True

    logger.info(
        "worker.processed",
        extra={
            "raw_event_id": str(raw_id),
            "entity_type": result.entity_type,
            "canonical_state": result.canonical_state,
        },
    )
    return True


async def run(classifier: SupportsClassify | None = None) -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s"
    )
    logger.info("worker.startup", extra={"llm_model": settings.llm_model})

    if classifier is None:
        llm = build_llm(settings)
        classifier = Classifier(llm)

    shutdown = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("worker.signal.shutdown")
        shutdown.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            # Windows / unusual environments; we don't deploy there but leave a hatch.
            loop.add_signal_handler(sig, _signal_handler)

    # Initial reaper sweep.
    try:
        await reap_stale(settings)
    except Exception:
        logger.exception("worker.reaper.initial_failed")

    last_reap = asyncio.get_event_loop().time()

    try:
        while not shutdown.is_set():
            now = asyncio.get_event_loop().time()
            if now - last_reap >= settings.worker_reaper_interval_s:
                try:
                    await reap_stale(settings)
                except Exception:
                    logger.exception("worker.reaper.failed")
                last_reap = now

            try:
                did_work = await process_one(classifier, settings)
            except Exception:
                logger.exception("worker.iteration_failed")
                did_work = False

            if not did_work:
                # Sleep with shutdown awareness so SIGTERM is responsive.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(shutdown.wait(), timeout=settings.worker_poll_interval_s)
    finally:
        await dispose_engine()
        logger.info("worker.shutdown.complete")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
