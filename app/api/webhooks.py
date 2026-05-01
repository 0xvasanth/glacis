from __future__ import annotations

import logging
from typing import Annotated, Any

import orjson
from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_session
from app.core.config import get_settings
from app.core.schemas import WebhookAck
from app.services import ingestion as ingestion_service

router = APIRouter(tags=["webhooks"])
logger = logging.getLogger(__name__)


async def _read_json_body(request: Request, max_bytes: int) -> dict[str, Any]:
    """Validate body length, decode JSON, ensure object root. Raises HTTPException."""
    body = await request.body()
    if len(body) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"payload exceeds {max_bytes} bytes",
        )
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty body")
    try:
        payload = orjson.loads(body)
    except orjson.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid JSON: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="payload root must be a JSON object",
        )
    return payload


@router.post(
    "/webhooks/{vendor}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookAck,
)
async def ingest_webhook(
    request: Request,
    vendor: Annotated[
        str,
        Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$"),
    ],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WebhookAck:
    payload = await _read_json_body(request, get_settings().api_request_max_bytes)
    result = await ingestion_service.insert_raw_event(session, vendor_hint=vendor, payload=payload)
    logger.info(
        "webhook.ingested",
        extra={
            "vendor": vendor,
            "raw_event_id": str(result.raw_event_id),
            "duplicate": result.duplicate,
            "duplicate_of": str(result.duplicate_of) if result.duplicate_of else None,
        },
    )
    return WebhookAck(
        raw_event_id=result.raw_event_id,
        duplicate=result.duplicate,
        duplicate_of=result.duplicate_of,
    )
