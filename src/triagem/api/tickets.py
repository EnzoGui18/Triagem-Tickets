from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from triagem.core.database import get_session
from triagem.schemas import TicketAcceptedResponse, TicketPayload
from triagem.ticket_ingestion import TicketPayloadConflictError, ingest_ticket

router = APIRouter(prefix="/tickets", tags=["tickets"])


@router.post("", response_model=TicketAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_ticket(
    payload: TicketPayload,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TicketAcceptedResponse:
    try:
        ticket = await ingest_ticket(session, payload)
    except TicketPayloadConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="external_id já está associado a um payload diferente",
        ) from error

    return TicketAcceptedResponse(
        id=ticket.id,
        external_id=ticket.external_id,
        status=ticket.status,
        received_at=ticket.received_at,
    )
