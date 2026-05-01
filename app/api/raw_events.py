from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select

from app.api.dependencies import get_classifier
from app.core.db import session_scope
from app.core.models import InvoiceEvent, ShipmentEvent
from app.core.schemas import RawEventRetryResult
from app.llm.classifier import SupportsClassify
from app.services.retry import RetryNotFoundError, RetryRefusedError, retry_raw_event

router = APIRouter(tags=["raw-events"])
logger = logging.getLogger(__name__)


@router.post(
    "/raw-events/{raw_event_id}/retry",
    response_model=RawEventRetryResult,
    summary="Re-run classification + persistence for a single raw_event (synchronous)",
)
async def retry_endpoint(
    raw_event_id: Annotated[uuid.UUID, Path(...)],
    classifier: Annotated[SupportsClassify, Depends(get_classifier)],
) -> RawEventRetryResult:
    try:
        raw = await retry_raw_event(classifier, raw_event_id)
    except RetryNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"raw_event {exc} not found"
        ) from exc
    except RetryRefusedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.reason) from exc
    except Exception as exc:
        # Service has already reset the row to status='pending' with last_error set.
        logger.exception("retry_endpoint.failed", extra={"raw_event_id": str(raw_event_id)})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"retry failed: {exc!r}",
        ) from exc

    # If the event reached an entity, surface its canonical_state so the
    # operator can confirm the outcome without a follow-up GET.
    canonical_state: str | None = None
    entity_type: str | None = None
    async with session_scope() as session:
        for event_model, etype in (
            (ShipmentEvent, "shipment"),
            (InvoiceEvent, "invoice"),
        ):
            row = (
                await session.execute(
                    select(event_model.canonical_state).where(event_model.raw_event_id == raw.id)
                )
            ).scalar_one_or_none()
            if row is not None:
                canonical_state = row
                entity_type = etype
                break

    return RawEventRetryResult(
        raw_event_id=raw.id,
        status=raw.status,
        attempts=raw.attempts,
        canonical_state=canonical_state,
        entity_type=entity_type,
        last_error=raw.last_error,
    )
