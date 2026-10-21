from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from triagem.models import JobStatus, JobType, ProcessingJob, Ticket, TicketStatus


@dataclass(frozen=True, slots=True)
class RequeuedClassificationJob:
    external_id: str
    failed_job_id: UUID
    new_job_id: UUID


async def requeue_failed_classification_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    max_attempts: int,
    external_id: str | None = None,
) -> list[RequeuedClassificationJob]:
    """Create a fresh classification job while preserving the failed job and its attempts."""
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")

    async with session_factory() as session, session.begin():
        failed_job_exists = exists(
            select(ProcessingJob.id).where(
                ProcessingJob.ticket_id == Ticket.id,
                ProcessingJob.job_type == JobType.CLASSIFICATION,
                ProcessingJob.status == JobStatus.FAILED,
            )
        )
        ticket_query = select(Ticket).where(failed_job_exists)
        if external_id is not None:
            ticket_query = ticket_query.where(Ticket.external_id == external_id)
        tickets = list(
            (
                await session.scalars(
                    ticket_query
                    .order_by(Ticket.received_at, Ticket.id)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )

        now = await session.scalar(select(func.clock_timestamp()))
        if now is None:  # pragma: no cover - PostgreSQL always returns a value
            raise RuntimeError("database clock is unavailable")

        requeued: list[RequeuedClassificationJob] = []
        for ticket in tickets:
            latest_job = await session.scalar(
                select(ProcessingJob)
                .where(
                    ProcessingJob.ticket_id == ticket.id,
                    ProcessingJob.job_type == JobType.CLASSIFICATION,
                )
                .order_by(ProcessingJob.created_at.desc(), ProcessingJob.id.desc())
                .limit(1)
                .with_for_update()
            )
            if latest_job is None or latest_job.status != JobStatus.FAILED:
                continue

            new_job = ProcessingJob(
                ticket_id=ticket.id,
                job_type=JobType.CLASSIFICATION,
                queue_priority=latest_job.queue_priority,
                max_attempts=max_attempts,
                available_at=now,
            )
            session.add(new_job)
            ticket.status = TicketStatus.RECEIVED
            ticket.updated_at = now
            await session.flush()
            requeued.append(
                RequeuedClassificationJob(
                    external_id=ticket.external_id,
                    failed_job_id=latest_job.id,
                    new_job_id=new_job.id,
                )
            )

        return requeued
