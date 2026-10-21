from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from triagem.core.config import get_settings
from triagem.core.database import get_session
from triagem.core.metrics import TICKETS_DUPLICATE, TICKETS_RECEIVED
from triagem.core.structured_logging import log_event
from triagem.models import Classification
from triagem.schemas import (
    ClassificationCostResponse,
    ClassificationUsageResponse,
    ClassifierIdentityResponse,
    TicketAcceptedResponse,
    TicketClassificationDetailResponse,
    TicketClassificationHistoryItemResponse,
    TicketClassificationHistoryResponse,
    TicketClassificationResponse,
    TicketCustomerResponse,
    TicketDetailResponse,
    TicketListFilters,
    TicketListItemResponse,
    TicketListResponse,
    TicketPayload,
    TicketProcessingResponse,
    TicketReclassificationAcceptedResponse,
    TicketTimelineResponse,
)
from triagem.ticket_ingestion import TicketPayloadConflictError, ingest_ticket
from triagem.ticket_queries import (
    ClassificationHistoryRecord,
    TicketQueryRecord,
    get_ticket_by_external_id,
    get_ticket_classification_history,
    list_tickets,
)
from triagem.ticket_reclassification import (
    ActiveClassificationJobError,
    TicketNotFoundError,
    enqueue_ticket_reclassification,
)
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


def _classification_usage(
    classification: Classification,
) -> ClassificationUsageResponse | None:
    if classification.input_tokens is None or classification.output_tokens is None:
        return None
    return ClassificationUsageResponse(
        input_tokens=classification.input_tokens,
        output_tokens=classification.output_tokens,
    )


def _classification_cost(
    classification: Classification,
) -> ClassificationCostResponse | None:
    if (
        classification.input_cost is None
        or classification.output_cost is None
        or classification.total_cost is None
    ):
        return None
    return ClassificationCostResponse(
        input=format(classification.input_cost, ".8f"),
        output=format(classification.output_cost, ".8f"),
        total=format(classification.total_cost, ".8f"),
    )


def _classification_detail_response(
    record: TicketQueryRecord,
) -> TicketClassificationDetailResponse | None:
    classification = record.classification
    if classification is None:
        return None
    return TicketClassificationDetailResponse(
        id=classification.id,
        category=classification.category,
        priority=classification.priority,
        sentiment=classification.sentiment,
        suggested_response=classification.suggested_response,
        created_at=classification.created_at,
        usage=_classification_usage(classification),
        cost_usd=_classification_cost(classification),
    )


def _classification_history_item_response(
    record: ClassificationHistoryRecord,
) -> TicketClassificationHistoryItemResponse:
    classification = record.classification
    classifier_version = record.classifier_version
    return TicketClassificationHistoryItemResponse(
        id=classification.id,
        classifier=ClassifierIdentityResponse(
            provider=classifier_version.provider,
            model=classifier_version.model_name,
            version=classifier_version.version,
        ),
        category=classification.category,
        priority=classification.priority,
        sentiment=classification.sentiment,
        suggested_response=classification.suggested_response,
        usage=_classification_usage(classification),
        cost_usd=_classification_cost(classification),
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
        queue_priority=ingestion.queue_priority,
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
        current_classification=_classification_detail_response(record),
        processing=processing,
    )


@router.get(
    "/{external_id}/classifications",
    response_model=TicketClassificationHistoryResponse,
)
async def get_classifications(
    external_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TicketClassificationHistoryResponse:
    history = await get_ticket_classification_history(session, external_id)
    if history is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ticket não encontrado")
    return TicketClassificationHistoryResponse(
        external_id=history.ticket.external_id,
        classifications=[
            _classification_history_item_response(item) for item in history.items
        ],
    )


@router.post(
    "/{external_id}/reclassify",
    response_model=TicketReclassificationAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reclassify_ticket(
    external_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TicketReclassificationAcceptedResponse:
    try:
        result = await enqueue_ticket_reclassification(
            session,
            external_id=external_id,
            max_attempts=get_settings().classification_max_attempts,
        )
    except TicketNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="ticket não encontrado",
        ) from error
    except ActiveClassificationJobError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ticket já possui job de classificação ativo",
        ) from error

    log_event(
        logger,
        logging.INFO,
        "ticket_reclassification_queued",
        external_id=result.external_id,
        job_id=result.job_id,
        queue_priority=result.queue_priority,
    )
    return TicketReclassificationAcceptedResponse(
        external_id=result.external_id,
        job_id=result.job_id,
        status=result.status,
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
