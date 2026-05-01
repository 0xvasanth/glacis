from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._event_helpers import events_to_out
from app.api.dependencies import get_session
from app.core.schemas import ShipmentOut
from app.services import shipments as shipments_service

router = APIRouter(tags=["shipments"])


@router.get("/shipments/{shipment_id}", response_model=ShipmentOut)
async def get_shipment(
    shipment_id: Annotated[uuid.UUID, Path(...)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ShipmentOut:
    shipment = await shipments_service.get_shipment(session, shipment_id)
    if shipment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="shipment not found")
    events = await shipments_service.list_events(session, shipment_id)
    return ShipmentOut(
        id=shipment.id,
        vendor=shipment.vendor,
        external_ref=shipment.external_ref,
        current_state=shipment.current_state,
        last_event_at=shipment.last_event_at,
        attributes=shipment.attributes,
        events=events_to_out(events),
    )
