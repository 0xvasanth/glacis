"""Invoice read-side service.

Pure functions over an AsyncSession. No HTTP knowledge; route handlers
translate `None` returns into 404 responses.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Invoice, InvoiceEvent


async def get_invoice(session: AsyncSession, invoice_id: uuid.UUID) -> Invoice | None:
    """Load an invoice by id. Returns None if not found."""
    return await session.get(Invoice, invoice_id)


async def list_events(session: AsyncSession, invoice_id: uuid.UUID) -> list[InvoiceEvent]:
    """Return the invoice's event history in chronological order."""
    result = await session.execute(
        select(InvoiceEvent)
        .where(InvoiceEvent.entity_id == invoice_id)
        .order_by(InvoiceEvent.event_at.asc())
    )
    return list(result.scalars().all())
