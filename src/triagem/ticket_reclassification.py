from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from triagem.models import JobStatus, JobType, ProcessingJob, Ticket
from triagem.scheduling import classification_queue_priority

ACTIVE_CLASSIFICATION_JOB_STATUSES = (
    JobStatus.PENDING,
    JobStatus.PROCESSING,
    JobStatus.RETRY_SCHEDULED,
)


class TicketNotFoundError(Exception):
    pass


class ActiveClassificationJobError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ReclassificationResult:
    external_id: str
    job_id: UUID
    status: JobStatus
    queue_priority: int


async def enqueue_ticket_reclassification(
    session: AsyncSession,
    *,
    external_id: str,
    max_attempts: int,
) -> ReclassificationResult:
    async with session.begin():
        ticket = await session.scalar(
            select(Ticket).where(Ticket.external_id == external_id).with_for_update()
        )
        if ticket is None:
            raise TicketNotFoundError

        active_job = await session.scalar(
            select(ProcessingJob.id)
            .where(
                ProcessingJob.ticket_id == ticket.id,
                ProcessingJob.job_type == JobType.CLASSIFICATION,
                ProcessingJob.status.in_(ACTIVE_CLASSIFICATION_JOB_STATUSES),
            )
            .limit(1)
        )
        if active_job is not None:
            raise ActiveClassificationJobError

        queue_priority = classification_queue_priority(
            subject=ticket.subject,
            message=ticket.message,
        )
        job = ProcessingJob(
            ticket_id=ticket.id,
            job_type=JobType.CLASSIFICATION,
            queue_priority=queue_priority,
            max_attempts=max_attempts,
        )
        session.add(job)
        await session.flush()

    return ReclassificationResult(
        external_id=ticket.external_id,
        job_id=job.id,
        status=job.status,
        queue_priority=queue_priority,
    )
