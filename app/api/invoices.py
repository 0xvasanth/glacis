from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._event_helpers import events_to_out
from app.api.dependencies import get_session
from app.core.schemas import InvoiceOut
from app.services import invoices as invoices_service

router = APIRouter(tags=["invoices"])


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
async def get_invoice(
    invoice_id: Annotated[uuid.UUID, Path(...)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InvoiceOut:
    invoice = await invoices_service.get_invoice(session, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invoice not found")
    events = await invoices_service.list_events(session, invoice_id)
    return InvoiceOut(
        id=invoice.id,
        vendor=invoice.vendor,
        external_ref=invoice.external_ref,
        current_state=invoice.current_state,
        last_event_at=invoice.last_event_at,
        attributes=invoice.attributes,
        events=events_to_out(events),
    )
