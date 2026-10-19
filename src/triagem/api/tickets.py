from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from triagem.core.database import get_session
from triagem.schemas import (
    TicketAcceptedResponse,
    TicketClassificationResponse,
    TicketCustomerResponse,
    TicketDetailResponse,
    TicketListFilters,
    TicketListItemResponse,
    TicketListResponse,
    TicketPayload,
    TicketProcessingResponse,
)
from triagem.ticket_ingestion import TicketPayloadConflictError, ingest_ticket
from triagem.ticket_queries import TicketQueryRecord, get_ticket_by_external_id, list_tickets

router = APIRouter(prefix="/tickets", tags=["tickets"])


def _classification_response(
    record: TicketQueryRecord,
) -> TicketClassificationResponse | None:
    classification = record.classification
    if classification is None:
        return None
    return TicketClassificationResponse(
        id=classification.id,
        category=classification.category,
        priority=classification.priority,
        sentiment=classification.sentiment,
        suggested_response=classification.suggested_response,
        created_at=classification.created_at,
    )


def _list_item_response(record: TicketQueryRecord) -> TicketListItemResponse:
    ticket = record.ticket
    return TicketListItemResponse(
        id=ticket.id,
        external_id=ticket.external_id,
        subject=ticket.subject,
        channel=ticket.channel,
        source_created_at=ticket.source_created_at,
        received_at=ticket.received_at,
        status=ticket.status,
        current_classification=_classification_response(record),
    )


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


@router.get("", response_model=TicketListResponse)
async def get_tickets(
    filters: Annotated[TicketListFilters, Query()],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TicketListResponse:
    page = await list_tickets(
        session,
        category=filters.category,
        priority=filters.priority,
        received_from=filters.received_from,
        received_until=filters.received_until,
        page=filters.page,
        page_size=filters.page_size,
    )
    total_pages = (page.total + filters.page_size - 1) // filters.page_size
    return TicketListResponse(
        items=[_list_item_response(record) for record in page.items],
        total=page.total,
        page=filters.page,
        page_size=filters.page_size,
        total_pages=total_pages,
    )


@router.get("/{external_id}", response_model=TicketDetailResponse)
async def get_ticket(
    external_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TicketDetailResponse:
    record = await get_ticket_by_external_id(session, external_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ticket não encontrado")

    ticket = record.ticket
    processing = None
    if record.processing_job is not None:
        job = record.processing_job
        processing = TicketProcessingResponse(
            job_id=job.id,
            status=job.status,
            attempt_count=job.attempt_count,
            max_attempts=job.max_attempts,
            available_at=job.available_at,
        )
    return TicketDetailResponse(
        id=ticket.id,
        external_id=ticket.external_id,
        customer=TicketCustomerResponse(
            external_id=ticket.customer_external_id,
            name=ticket.customer_name,
            email=ticket.customer_email,
        ),
        subject=ticket.subject,
        message=ticket.message,
        channel=ticket.channel,
        source_created_at=ticket.source_created_at,
        received_at=ticket.received_at,
        status=ticket.status,
        current_classification=_classification_response(record),
        processing=processing,
    )
