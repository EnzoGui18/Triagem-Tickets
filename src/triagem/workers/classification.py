from __future__ import annotations

import asyncio
import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from triagem.classifiers import ClassificationResult, Classifier, FakeClassifier
from triagem.core.config import get_settings
from triagem.core.database import async_session_factory, engine
from triagem.models import (
    Classification,
    ClassifierVersion,
    JobStatus,
    JobType,
    ProcessingJob,
    Ticket,
    TicketStatus,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReservedJob:
    id: UUID
    ticket_id: UUID
    locked_at: datetime
    attempt_count: int


class ClassificationWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        classifier: Classifier,
        *,
        worker_id: str,
        lease_duration: timedelta,
        retry_delay: timedelta,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if not worker_id or len(worker_id) > 255:
            raise ValueError("worker_id must have between 1 and 255 characters")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if retry_delay < timedelta(0):
            raise ValueError("retry_delay cannot be negative")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")

        self._session_factory = session_factory
        self._classifier = classifier
        self.worker_id = worker_id
        self.lease_duration = lease_duration
        self.retry_delay = retry_delay
        self.poll_interval_seconds = poll_interval_seconds

    async def reserve_next_job(self) -> ReservedJob | None:
        async with self._session_factory() as session, session.begin():
            now = await session.scalar(select(func.clock_timestamp()))
            if now is None:  # pragma: no cover - PostgreSQL always returns a value
                raise RuntimeError("database clock is unavailable")

            await self._recover_expired_leases(session, now)

            job = await session.scalar(
                select(ProcessingJob)
                .where(
                    ProcessingJob.job_type == JobType.CLASSIFICATION,
                    ProcessingJob.status.in_([JobStatus.PENDING, JobStatus.RETRY_SCHEDULED]),
                    ProcessingJob.available_at <= now,
                    ProcessingJob.attempt_count < ProcessingJob.max_attempts,
                )
                .order_by(
                    ProcessingJob.queue_priority.desc(),
                    ProcessingJob.available_at,
                    ProcessingJob.created_at,
                    ProcessingJob.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None

            job.status = JobStatus.PROCESSING
            job.attempt_count += 1
            job.locked_at = now
            job.locked_by = self.worker_id
            job.last_error_code = None
            job.last_error_message = None
            job.updated_at = now
            await session.execute(
                update(Ticket)
                .where(Ticket.id == job.ticket_id)
                .values(status=TicketStatus.PROCESSING, updated_at=now)
            )

            return ReservedJob(
                id=job.id,
                ticket_id=job.ticket_id,
                locked_at=now,
                attempt_count=job.attempt_count,
            )

    async def run_once(self) -> bool:
        reserved_job = await self.reserve_next_job()
        if reserved_job is None:
            return False

        try:
            ticket = await self._load_ticket(reserved_job.ticket_id)
            result = await self._classifier.classify(ticket)
        except Exception as error:
            recorded = await self._record_failure(reserved_job, error)
            if recorded:
                logger.warning(
                    "classification_job_failed job_id=%s worker_id=%s error_type=%s",
                    reserved_job.id,
                    self.worker_id,
                    type(error).__name__,
                )
            return True

        classification_id = await self._complete_job(reserved_job, result)
        if classification_id is None:
            logger.info(
                "classification_job_lease_lost job_id=%s worker_id=%s",
                reserved_job.id,
                self.worker_id,
            )
        else:
            logger.info(
                "classification_job_completed job_id=%s classification_id=%s worker_id=%s",
                reserved_job.id,
                classification_id,
                self.worker_id,
            )
        return True

    async def run_until_idle(self) -> int:
        processed = 0
        while await self.run_once():
            processed += 1
        return processed

    async def run_forever(self) -> None:
        while True:
            if not await self.run_once():
                await asyncio.sleep(self.poll_interval_seconds)

    async def _recover_expired_leases(self, session: AsyncSession, now: datetime) -> None:
        lease_expired = and_(
            ProcessingJob.job_type == JobType.CLASSIFICATION,
            ProcessingJob.status == JobStatus.PROCESSING,
            or_(
                ProcessingJob.locked_at.is_(None),
                ProcessingJob.locked_at <= now - self.lease_duration,
            ),
        )

        exhausted_result = await session.execute(
            update(ProcessingJob)
            .where(lease_expired, ProcessingJob.attempt_count >= ProcessingJob.max_attempts)
            .values(
                status=JobStatus.FAILED,
                locked_at=None,
                locked_by=None,
                last_error_code="lease_exhausted",
                last_error_message="worker lease expired after the final attempt",
                completed_at=now,
                updated_at=now,
            )
            .returning(ProcessingJob.ticket_id)
        )
        exhausted_ticket_ids = list(exhausted_result.scalars())
        if exhausted_ticket_ids:
            await session.execute(
                update(Ticket)
                .where(Ticket.id.in_(exhausted_ticket_ids))
                .values(status=TicketStatus.FAILED, updated_at=now)
            )

        await session.execute(
            update(ProcessingJob)
            .where(lease_expired, ProcessingJob.attempt_count < ProcessingJob.max_attempts)
            .values(
                status=JobStatus.RETRY_SCHEDULED,
                available_at=now,
                locked_at=None,
                locked_by=None,
                last_error_code="lease_expired",
                last_error_message="worker lease expired before completion",
                updated_at=now,
            )
        )

    async def _load_ticket(self, ticket_id: UUID) -> Ticket:
        async with self._session_factory() as session:
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                raise RuntimeError("job ticket was not found")
            return ticket

    async def _complete_job(
        self,
        reserved_job: ReservedJob,
        result: ClassificationResult,
    ) -> UUID | None:
        async with self._session_factory() as session, session.begin():
            job = await self._owned_job(session, reserved_job)
            if job is None:
                return None

            now = await session.scalar(select(func.clock_timestamp()))
            if now is None:  # pragma: no cover - PostgreSQL always returns a value
                raise RuntimeError("database clock is unavailable")
            classifier_version_id = await self._classifier_version_id(session)
            classification = Classification(
                ticket_id=job.ticket_id,
                classifier_version_id=classifier_version_id,
                category=result.category,
                priority=result.priority,
                sentiment=result.sentiment,
                suggested_response=result.suggested_response,
                provider_request_id=result.provider_request_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                provider_latency_ms=result.provider_latency_ms,
            )
            session.add(classification)
            await session.flush()

            job.classification_id = classification.id
            job.status = JobStatus.COMPLETED
            job.locked_at = None
            job.locked_by = None
            job.completed_at = now
            job.updated_at = now
            await session.execute(
                update(Ticket)
                .where(Ticket.id == job.ticket_id)
                .values(status=TicketStatus.CLASSIFIED, updated_at=now)
            )
            return classification.id

    async def _record_failure(self, reserved_job: ReservedJob, error: Exception) -> bool:
        async with self._session_factory() as session, session.begin():
            job = await self._owned_job(session, reserved_job)
            if job is None:
                return False

            now = await session.scalar(select(func.clock_timestamp()))
            if now is None:  # pragma: no cover - PostgreSQL always returns a value
                raise RuntimeError("database clock is unavailable")
            job.locked_at = None
            job.locked_by = None
            job.last_error_code = type(error).__name__[:100]
            job.last_error_message = "classification attempt failed"
            job.updated_at = now

            if job.attempt_count < job.max_attempts:
                job.status = JobStatus.RETRY_SCHEDULED
                job.available_at = now + self.retry_delay
            else:
                job.status = JobStatus.FAILED
                job.completed_at = now
                await session.execute(
                    update(Ticket)
                    .where(Ticket.id == job.ticket_id)
                    .values(status=TicketStatus.FAILED, updated_at=now)
                )
            return True

    async def _owned_job(
        self,
        session: AsyncSession,
        reserved_job: ReservedJob,
    ) -> ProcessingJob | None:
        return await session.scalar(
            select(ProcessingJob)
            .where(
                ProcessingJob.id == reserved_job.id,
                ProcessingJob.status == JobStatus.PROCESSING,
                ProcessingJob.locked_by == self.worker_id,
                ProcessingJob.attempt_count == reserved_job.attempt_count,
            )
            .with_for_update()
        )

    async def _classifier_version_id(self, session: AsyncSession) -> UUID:
        identity = self._classifier.identity
        inserted_id = await session.scalar(
            insert(ClassifierVersion)
            .values(
                provider=identity.provider,
                model_name=identity.model_name,
                version=identity.version,
                system_prompt=identity.system_prompt,
                output_schema_version=identity.output_schema_version,
            )
            .on_conflict_do_nothing(constraint="uq_classifier_versions_identity")
            .returning(ClassifierVersion.id)
        )
        if inserted_id is not None:
            return inserted_id

        existing_id = await session.scalar(
            select(ClassifierVersion.id).where(
                ClassifierVersion.provider == identity.provider,
                ClassifierVersion.model_name == identity.model_name,
                ClassifierVersion.version == identity.version,
            )
        )
        if existing_id is None:  # pragma: no cover - protected by the unique constraint
            raise RuntimeError("classifier version could not be resolved")
        return existing_id


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"


async def _run_worker() -> None:
    settings = get_settings()
    worker = ClassificationWorker(
        async_session_factory,
        FakeClassifier(),
        worker_id=_worker_id(),
        lease_duration=timedelta(seconds=settings.classification_worker_lease_seconds),
        retry_delay=timedelta(seconds=settings.classification_worker_retry_delay_seconds),
        poll_interval_seconds=settings.classification_worker_poll_interval_seconds,
    )
    try:
        await worker.run_forever()
    finally:
        await engine.dispose()


def main() -> None:
    logging.basicConfig(level=get_settings().log_level)
    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
