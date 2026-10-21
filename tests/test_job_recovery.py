from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from triagem.classifiers import FakeClassifier
from triagem.job_recovery import requeue_failed_classification_jobs
from triagem.models import (
    Classification,
    ClassificationAttempt,
    JobStatus,
    JobType,
    ProcessingJob,
    Ticket,
    TicketStatus,
)
from triagem.tools.requeue_failed import _parse_args
from triagem.workers.classification import ClassificationWorker


@pytest_asyncio.fixture
async def recovery_session_factory(
    migrated_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    test_engine = create_async_engine(migrated_database_url)
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session, session.begin():
        now = await session.scalar(select(func.clock_timestamp()))
        assert now is not None
        await session.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.job_type == JobType.CLASSIFICATION,
                ProcessingJob.status.in_(
                    (JobStatus.PENDING, JobStatus.PROCESSING, JobStatus.RETRY_SCHEDULED)
                ),
            )
            .values(
                status=JobStatus.FAILED,
                locked_at=None,
                locked_by=None,
                completed_at=now,
                updated_at=now,
            )
        )
    try:
        yield factory
    finally:
        await test_engine.dispose()


async def _seed_failed_job(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID, str]:
    async with session_factory() as session, session.begin():
        now = await session.scalar(select(func.clock_timestamp()))
        assert now is not None
        external_id = f"requeue-failed-{uuid4()}"
        ticket = Ticket(
            external_id=external_id,
            customer_external_id=f"customer-{uuid4()}",
            customer_name="Cliente Requeue",
            customer_email="requeue@example.com",
            subject="Pedido atrasado",
            message="Meu pedido ainda não chegou.",
            channel="web",
            source_created_at=datetime.now(UTC),
            status=TicketStatus.FAILED,
            payload_hash="f" * 64,
        )
        session.add(ticket)
        await session.flush()
        failed_job = ProcessingJob(
            ticket_id=ticket.id,
            job_type=JobType.CLASSIFICATION,
            queue_priority=100,
            status=JobStatus.FAILED,
            attempt_count=3,
            max_attempts=3,
            available_at=now,
            last_error_code="llm_http_500",
            last_error_message="retryable classification failure",
            completed_at=now,
            created_at=now - timedelta(minutes=1),
            updated_at=now,
        )
        session.add(failed_job)
        await session.flush()
        session.add_all(
            [
                ClassificationAttempt(
                    processing_job_id=failed_job.id,
                    attempt_number=attempt_number,
                    status=(
                        JobStatus.FAILED
                        if attempt_number == 3
                        else JobStatus.RETRY_SCHEDULED
                    ),
                    started_at=now - timedelta(seconds=4 - attempt_number),
                    finished_at=now - timedelta(seconds=3 - attempt_number),
                    error_code="llm_http_500",
                    error_message="retryable classification failure",
                )
                for attempt_number in range(1, 4)
            ]
        )
        return ticket.id, failed_job.id, external_id


def _worker(
    session_factory: async_sessionmaker[AsyncSession],
) -> ClassificationWorker:
    return ClassificationWorker(
        session_factory,
        FakeClassifier(),
        worker_id=f"requeue-worker-{uuid4()}",
        lease_duration=timedelta(seconds=30),
        retry_delay=timedelta(0),
        poll_interval_seconds=0.01,
    )


async def test_requeue_creates_fresh_budget_and_worker_completes_without_losing_history(
    recovery_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ticket_id, failed_job_id, external_id = await _seed_failed_job(
        recovery_session_factory
    )

    requeued = await requeue_failed_classification_jobs(
        recovery_session_factory,
        external_id=external_id,
        max_attempts=8,
    )

    assert len(requeued) == 1
    assert requeued[0].failed_job_id == failed_job_id
    new_job_id = requeued[0].new_job_id
    async with recovery_session_factory() as session:
        ticket = await session.get(Ticket, ticket_id)
        failed_job = await session.get(ProcessingJob, failed_job_id)
        new_job = await session.get(ProcessingJob, new_job_id)
        old_attempts = list(
            (
                await session.scalars(
                    select(ClassificationAttempt)
                    .where(ClassificationAttempt.processing_job_id == failed_job_id)
                    .order_by(ClassificationAttempt.attempt_number)
                )
            ).all()
        )

    assert ticket is not None
    assert ticket.status == TicketStatus.RECEIVED
    assert failed_job is not None
    assert failed_job.status == JobStatus.FAILED
    assert failed_job.attempt_count == failed_job.max_attempts == 3
    assert failed_job.last_error_code == "llm_http_500"
    assert [attempt.attempt_number for attempt in old_attempts] == [1, 2, 3]
    assert new_job is not None
    assert new_job.status == JobStatus.PENDING
    assert new_job.attempt_count == 0
    assert new_job.max_attempts == 8
    assert new_job.queue_priority == 100
    assert new_job.locked_at is None
    assert new_job.locked_by is None
    assert new_job.last_error_code is None
    assert new_job.completed_at is None

    assert await _worker(recovery_session_factory).run_once() is True

    async with recovery_session_factory() as session:
        ticket = await session.get(Ticket, ticket_id)
        failed_job = await session.get(ProcessingJob, failed_job_id)
        new_job = await session.get(ProcessingJob, new_job_id)
        old_attempt_count = await session.scalar(
            select(func.count(ClassificationAttempt.id)).where(
                ClassificationAttempt.processing_job_id == failed_job_id
            )
        )
        new_attempt_count = await session.scalar(
            select(func.count(ClassificationAttempt.id)).where(
                ClassificationAttempt.processing_job_id == new_job_id
            )
        )
        classification_count = await session.scalar(
            select(func.count(Classification.id)).where(
                Classification.ticket_id == ticket_id
            )
        )

    assert ticket is not None
    assert ticket.status == TicketStatus.CLASSIFIED
    assert failed_job is not None
    assert failed_job.status == JobStatus.FAILED
    assert new_job is not None
    assert new_job.status == JobStatus.COMPLETED
    assert old_attempt_count == 3
    assert new_attempt_count == 1
    assert classification_count == 1


async def test_requeue_does_not_reopen_ticket_whose_latest_job_completed(
    recovery_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ticket_id, failed_job_id, external_id = await _seed_failed_job(
        recovery_session_factory
    )
    async with recovery_session_factory() as session, session.begin():
        now = await session.scalar(select(func.clock_timestamp()))
        assert now is not None
        session.add(
            ProcessingJob(
                ticket_id=ticket_id,
                job_type=JobType.CLASSIFICATION,
                status=JobStatus.COMPLETED,
                attempt_count=1,
                max_attempts=8,
                available_at=now,
                completed_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        ticket = await session.get(Ticket, ticket_id)
        assert ticket is not None
        ticket.status = TicketStatus.CLASSIFIED

    requeued = await requeue_failed_classification_jobs(
        recovery_session_factory,
        external_id=external_id,
        max_attempts=8,
    )

    assert requeued == []
    async with recovery_session_factory() as session:
        jobs = list(
            (
                await session.scalars(
                    select(ProcessingJob).where(ProcessingJob.ticket_id == ticket_id)
                )
            ).all()
        )
    assert len(jobs) == 2
    assert {job.id for job in jobs} >= {failed_job_id}
    assert any(job.status == JobStatus.COMPLETED for job in jobs)


def test_requeue_cli_requires_exactly_one_target() -> None:
    by_external_id = _parse_args(["--external-id", "ATD-2026001"])
    all_failed = _parse_args(["--all"])

    assert by_external_id.external_id == "ATD-2026001"
    assert by_external_id.all_failed is False
    assert all_failed.external_id is None
    assert all_failed.all_failed is True
    with pytest.raises(SystemExit):
        _parse_args([])
    with pytest.raises(SystemExit):
        _parse_args(["--all", "--external-id", "ATD-2026001"])
