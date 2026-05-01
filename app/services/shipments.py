"""Shipment read-side service.

Pure functions over an AsyncSession. No HTTP knowledge; route handlers
translate `None` returns into 404 responses.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Shipment, ShipmentEvent


async def get_shipment(session: AsyncSession, shipment_id: uuid.UUID) -> Shipment | None:
    """Load a shipment by id. Returns None if not found."""
    return await session.get(Shipment, shipment_id)


async def list_events(session: AsyncSession, shipment_id: uuid.UUID) -> list[ShipmentEvent]:
    """Return the shipment's event history in chronological order."""
    result = await session.execute(
        select(ShipmentEvent)
        .where(ShipmentEvent.entity_id == shipment_id)
        .order_by(ShipmentEvent.event_at.asc())
    )
    return list(result.scalars().all())
