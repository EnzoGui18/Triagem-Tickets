from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from triagem.models import (
    Classification,
    ClassificationCategory,
    JobStatus,
    JobType,
    ProcessingJob,
    Ticket,
    TicketPriority,
)


@dataclass(frozen=True, slots=True)
class TicketQueryRecord:
    ticket: Ticket
    classification: Classification | None
    processing_job: ProcessingJob | None = None


@dataclass(frozen=True, slots=True)
class TicketPage:
    items: list[TicketQueryRecord]
    total: int


def _current_classification_query() -> tuple[
    Select[Ticket, Classification], type[Classification]
]:
    ranked_classifications = (
        select(
            Classification.id.label("classification_id"),
            Classification.ticket_id.label("ticket_id"),
            func.row_number()
            .over(
                partition_by=Classification.ticket_id,
                order_by=(Classification.created_at.desc(), Classification.id.desc()),
            )
            .label("position"),
        )
        .subquery("ranked_classifications")
    )
    current_classification = aliased(Classification, name="current_classification")
    query = (
        select(Ticket, current_classification)
        .outerjoin(
            ranked_classifications,
            and_(
                ranked_classifications.c.ticket_id == Ticket.id,
                ranked_classifications.c.position == 1,
            ),
        )
        .outerjoin(
            current_classification,
            current_classification.id == ranked_classifications.c.classification_id,
        )
    )
    return query, current_classification


async def get_ticket_by_external_id(
    session: AsyncSession,
    external_id: str,
) -> TicketQueryRecord | None:
    ticket = await session.scalar(select(Ticket).where(Ticket.external_id == external_id))
    if ticket is None:
        return None

    classification = await session.scalar(
        select(Classification)
        .where(Classification.ticket_id == ticket.id)
        .order_by(Classification.created_at.desc(), Classification.id.desc())
        .limit(1)
    )
    processing_job = await session.scalar(
        select(ProcessingJob)
        .where(
            ProcessingJob.ticket_id == ticket.id,
            ProcessingJob.job_type == JobType.CLASSIFICATION,
            ProcessingJob.status.in_(
                (JobStatus.PENDING, JobStatus.PROCESSING, JobStatus.RETRY_SCHEDULED)
            ),
        )
        .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
        .limit(1)
    )
    return TicketQueryRecord(
        ticket=ticket,
        classification=classification,
        processing_job=processing_job,
    )


async def list_tickets(
    session: AsyncSession,
    *,
    category: ClassificationCategory | None,
    priority: TicketPriority | None,
    received_from: datetime | None,
    received_until: datetime | None,
    page: int,
    page_size: int,
) -> TicketPage:
    query, current_classification = _current_classification_query()

    filters = []
    if category is not None:
        filters.append(current_classification.category == category)
    if priority is not None:
        filters.append(current_classification.priority == priority)
    if received_from is not None:
        filters.append(Ticket.received_at >= received_from)
    if received_until is not None:
        filters.append(Ticket.received_at <= received_until)

    filtered_query = query.where(*filters)
    count_query = select(func.count()).select_from(filtered_query.order_by(None).subquery())
    total = int(await session.scalar(count_query) or 0)

    result = await session.execute(
        filtered_query.order_by(Ticket.received_at.desc(), Ticket.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return TicketPage(
        items=[
            TicketQueryRecord(ticket=ticket, classification=classification)
            for ticket, classification in result.all()
        ],
        total=total,
    )
