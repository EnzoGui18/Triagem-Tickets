from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from triagem.core.database import get_session
from triagem.core.metrics import TICKETS_DUPLICATE, TICKETS_RECEIVED
from triagem.core.structured_logging import log_event
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
    TicketTimelineResponse,
)
from triagem.ticket_ingestion import TicketPayloadConflictError, ingest_ticket
from triagem.ticket_queries import TicketQueryRecord, get_ticket_by_external_id, list_tickets
from triagem.ticket_timeline import get_ticket_timeline

router = APIRouter(prefix="/tickets", tags=["tickets"])
logger = logging.getLogger(__name__)


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
        ingestion = await ingest_ticket(session, payload)
    except TicketPayloadConflictError as error:
        log_event(
            logger,
            logging.WARNING,
            "ticket_conflict",
            external_id=payload.external_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="external_id já está associado a um payload diferente",
        ) from error

    ticket = ingestion.ticket
    if ingestion.duplicate:
        TICKETS_DUPLICATE.inc()
        event = "ticket_duplicate"
    else:
        TICKETS_RECEIVED.inc()
        event = "ticket_received"
    log_event(
        logger,
        logging.INFO,
        event,
        external_id=ticket.external_id,
        ticket_id=ticket.id,
        job_id=ingestion.job_id,
    )

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


@router.get(
    "/{external_id}/timeline",
    response_model=TicketTimelineResponse,
    response_model_exclude_none=True,
)
async def get_timeline(
    external_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TicketTimelineResponse:
    timeline = await get_ticket_timeline(session, external_id)
    if timeline is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ticket não encontrado")
    return timeline
