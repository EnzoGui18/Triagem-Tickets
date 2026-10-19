import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from triagem.classifiers import (
    ClassificationResult,
    Classifier,
    ClassifierIdentity,
    FakeClassifier,
)
from triagem.llm.circuit_breaker import CircuitOpenError, CircuitProbeFailureError
from triagem.models import (
    Classification,
    ClassificationCategory,
    ClassifierVersion,
    JobStatus,
    JobType,
    ProcessingJob,
    Sentiment,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from triagem.workers.classification import ClassificationWorker


@pytest_asyncio.fixture
async def worker_session_factory(
    migrated_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    test_engine = create_async_engine(migrated_database_url)
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await test_engine.dispose()


async def _enqueue_job(
    session_factory: async_sessionmaker[AsyncSession],
    external_id: str,
) -> tuple[UUID, UUID]:
    async with session_factory() as session, session.begin():
        database_now = await session.scalar(select(func.clock_timestamp()))
        if database_now is None:  # pragma: no cover - PostgreSQL always returns a value
            raise RuntimeError("database clock is unavailable")
        ticket = Ticket(
            external_id=external_id,
            customer_external_id=f"customer-{external_id}",
            customer_name="Cliente Worker",
            customer_email="worker@example.com",
            subject="Assunto de teste",
            message="Mensagem para classificação fake.",
            channel="web",
            source_created_at=datetime.now(UTC),
            payload_hash="c" * 64,
        )
        session.add(ticket)
        await session.flush()
        job = ProcessingJob(
            ticket_id=ticket.id,
            job_type=JobType.CLASSIFICATION,
            queue_priority=10,
            available_at=database_now - timedelta(minutes=1),
        )
        session.add(job)
        await session.flush()
        return job.id, ticket.id


async def _enqueue_reclassification_job(
    session_factory: async_sessionmaker[AsyncSession],
    ticket_id: UUID,
) -> UUID:
    async with session_factory() as session, session.begin():
        now = await session.scalar(select(func.clock_timestamp()))
        assert now is not None
        job = ProcessingJob(
            ticket_id=ticket_id,
            job_type=JobType.CLASSIFICATION,
            queue_priority=10,
            available_at=now - timedelta(seconds=1),
        )
        session.add(job)
        await session.flush()
        return job.id


def _worker(
    session_factory: async_sessionmaker[AsyncSession],
    worker_id: str,
    *,
    classifier: Classifier | None = None,
) -> ClassificationWorker:
    return ClassificationWorker(
        session_factory,
        classifier or FakeClassifier(),
        worker_id=worker_id,
        lease_duration=timedelta(seconds=30),
        retry_delay=timedelta(0),
        poll_interval_seconds=0.01,
    )


@pytest.mark.parametrize("iteration", range(5))
async def test_concurrent_workers_complete_each_job_once(
    iteration: int,
    worker_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ticket_ids: list[UUID] = []
    job_ids: list[UUID] = []
    for index in range(12):
        job_id, ticket_id = await _enqueue_job(
            worker_session_factory,
            f"worker-concurrent-{iteration}-{index}-{uuid4()}",
        )
        job_ids.append(job_id)
        ticket_ids.append(ticket_id)

    first_worker = _worker(worker_session_factory, "worker-concurrent-a")
    second_worker = _worker(worker_session_factory, "worker-concurrent-b")
    await asyncio.gather(first_worker.run_until_idle(), second_worker.run_until_idle())

    async with worker_session_factory() as session:
        jobs = list(
            (
                await session.scalars(select(ProcessingJob).where(ProcessingJob.id.in_(job_ids)))
            ).all()
        )
        classification_counts = dict(
            (
                await session.execute(
                    select(Classification.ticket_id, func.count(Classification.id))
                    .where(Classification.ticket_id.in_(ticket_ids))
                    .group_by(Classification.ticket_id)
                )
            ).all()
        )

    unexpected_statuses = {
        str(job.id): job.status.value for job in jobs if job.status != JobStatus.COMPLETED
    }
    assert len(jobs) == 12
    assert unexpected_statuses == {}
    assert all(job.classification_id is not None for job in jobs)
    assert all(job.attempt_count == 1 for job in jobs)
    assert classification_counts == dict.fromkeys(ticket_ids, 1)


async def test_expired_worker_lease_is_recovered_after_crash(
    worker_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id, ticket_id = await _enqueue_job(
        worker_session_factory,
        f"worker-crash-{uuid4()}",
    )
    crashed_worker = _worker(worker_session_factory, "worker-that-crashed")
    reserved = await crashed_worker.reserve_next_job()

    assert reserved is not None
    assert reserved.id == job_id

    replacement_worker = _worker(worker_session_factory, "replacement-worker")
    assert await replacement_worker.run_once() is False

    async with worker_session_factory() as session, session.begin():
        await session.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id)
            .values(locked_at=datetime.now(UTC) - timedelta(minutes=1))
        )

    assert await replacement_worker.run_once() is True

    async with worker_session_factory() as session:
        job = await session.get(ProcessingJob, job_id)
        ticket = await session.get(Ticket, ticket_id)
        classification_count = await session.scalar(
            select(func.count(Classification.id)).where(Classification.ticket_id == ticket_id)
        )

    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.attempt_count == 2
    assert job.locked_at is None
    assert job.locked_by is None
    assert ticket is not None
    assert ticket.status == TicketStatus.CLASSIFIED
    assert classification_count == 1


class FailingFakeClassifier(FakeClassifier):
    async def classify(self, ticket: Ticket) -> ClassificationResult:
        del ticket
        raise RuntimeError("simulated classifier failure")


class BlockingFakeClassifier(FakeClassifier):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def classify(self, ticket: Ticket) -> ClassificationResult:
        self.started.set()
        await self.release.wait()
        return await super().classify(ticket)


class CircuitDeferringClassifier(FakeClassifier):
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def classify(self, ticket: Ticket) -> ClassificationResult:
        del ticket
        raise self._error


class VersionedFakeClassifier(FakeClassifier):
    def __init__(
        self,
        *,
        version: str,
        category: ClassificationCategory,
        priority: TicketPriority,
        system_prompt: str | None = None,
    ) -> None:
        self.identity = ClassifierIdentity(
            provider="version-test",
            model_name="versioned-fake",
            version=version,
            system_prompt=system_prompt or f"prompt-{version}",
            output_schema_version="1",
        )
        self._category = category
        self._priority = priority

    async def classify(self, ticket: Ticket) -> ClassificationResult:
        del ticket
        return ClassificationResult(
            category=self._category,
            priority=self._priority,
            sentiment=Sentiment.NEUTRAL,
            suggested_response=f"Resposta da versão {self.identity.version}.",
            input_tokens=10,
            output_tokens=5,
        )


async def test_stale_worker_cannot_complete_after_lease_is_reassigned(
    worker_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id, ticket_id = await _enqueue_job(
        worker_session_factory,
        f"worker-stale-{uuid4()}",
    )
    blocking_classifier = BlockingFakeClassifier()
    stale_worker = _worker(
        worker_session_factory,
        "stale-worker",
        classifier=blocking_classifier,
    )
    stale_task = asyncio.create_task(stale_worker.run_once())
    await asyncio.wait_for(blocking_classifier.started.wait(), timeout=2)

    async with worker_session_factory() as session, session.begin():
        await session.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id)
            .values(locked_at=datetime.now(UTC) - timedelta(minutes=1))
        )

    replacement_worker = _worker(worker_session_factory, "new-lease-owner")
    assert await replacement_worker.run_once() is True
    blocking_classifier.release.set()
    assert await stale_task is True

    async with worker_session_factory() as session:
        job = await session.get(ProcessingJob, job_id)
        classification_count = await session.scalar(
            select(func.count(Classification.id)).where(Classification.ticket_id == ticket_id)
        )

    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.attempt_count == 2
    assert classification_count == 1


async def test_retry_can_be_resumed_by_a_new_worker_instance(
    worker_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id, ticket_id = await _enqueue_job(
        worker_session_factory,
        f"worker-restart-{uuid4()}",
    )
    first_worker = _worker(
        worker_session_factory,
        "worker-before-restart",
        classifier=FailingFakeClassifier(),
    )

    assert await first_worker.run_once() is True
    async with worker_session_factory() as session:
        retry_job = await session.get(ProcessingJob, job_id)
        assert retry_job is not None
        assert retry_job.status == JobStatus.RETRY_SCHEDULED
        assert retry_job.attempt_count == 1
        assert retry_job.locked_at is None
        assert retry_job.locked_by is None

    restarted_worker = _worker(worker_session_factory, "worker-after-restart")
    assert await restarted_worker.run_once() is True

    async with worker_session_factory() as session:
        completed_job = await session.get(ProcessingJob, job_id)
        classification = await session.scalar(
            select(Classification).where(Classification.ticket_id == ticket_id)
        )

    assert completed_job is not None
    assert completed_job.status == JobStatus.COMPLETED
    assert completed_job.attempt_count == 2
    assert classification is not None
    assert classification.category == ClassificationCategory.OTHER
    assert classification.priority == TicketPriority.MEDIUM
    assert classification.sentiment == Sentiment.NEUTRAL


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (CircuitOpenError(retry_after_seconds=12), "llm_circuit_open"),
        (
            CircuitProbeFailureError(retry_after_seconds=12),
            "llm_circuit_probe_failed",
        ),
    ],
)
async def test_circuit_deferral_does_not_consume_job_attempt(
    error: Exception,
    expected_code: str,
    worker_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id, _ = await _enqueue_job(
        worker_session_factory,
        f"worker-circuit-deferral-{uuid4()}",
    )
    worker = _worker(
        worker_session_factory,
        "worker-circuit-open",
        classifier=CircuitDeferringClassifier(error),
    )

    assert await worker.run_once() is True

    async with worker_session_factory() as session:
        job = await session.get(ProcessingJob, job_id)

    assert job is not None
    assert job.status == JobStatus.RETRY_SCHEDULED
    assert job.attempt_count == 0
    assert job.last_error_code == expected_code
    assert (job.available_at - job.updated_at).total_seconds() >= 12


async def test_reclassification_preserves_history_and_exact_classifier_version(
    worker_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_job_id, ticket_id = await _enqueue_job(
        worker_session_factory,
        f"worker-reclassification-{uuid4()}",
    )
    first_worker = _worker(
        worker_session_factory,
        "worker-classifier-v1",
        classifier=VersionedFakeClassifier(
            version="v1",
            category=ClassificationCategory.OTHER,
            priority=TicketPriority.MEDIUM,
        ),
    )
    assert await first_worker.run_once() is True

    second_job_id = await _enqueue_reclassification_job(
        worker_session_factory,
        ticket_id,
    )

    second_worker = _worker(
        worker_session_factory,
        "worker-classifier-v2",
        classifier=VersionedFakeClassifier(
            version="v2",
            category=ClassificationCategory.DELIVERY,
            priority=TicketPriority.HIGH,
        ),
    )
    assert await second_worker.run_once() is True

    async with worker_session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(Classification, ClassifierVersion)
                    .join(
                        ClassifierVersion,
                        Classification.classifier_version_id == ClassifierVersion.id,
                    )
                    .where(Classification.ticket_id == ticket_id)
                    .order_by(ClassifierVersion.version)
                )
            ).all()
        )
        jobs = list(
            (
                await session.scalars(
                    select(ProcessingJob).where(
                        ProcessingJob.id.in_([first_job_id, second_job_id])
                    )
                )
            ).all()
        )

    assert len(rows) == 2
    first_classification, first_version = rows[0]
    second_classification, second_version = rows[1]
    assert first_classification.id != second_classification.id
    assert first_version.version == "v1"
    assert first_classification.category == ClassificationCategory.OTHER
    assert second_version.version == "v2"
    assert second_classification.category == ClassificationCategory.DELIVERY
    assert {job.status for job in jobs} == {JobStatus.COMPLETED}
    assert {job.classification_id for job in jobs} == {
        first_classification.id,
        second_classification.id,
    }


async def test_classifier_version_identity_cannot_change_its_definition(
    worker_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, ticket_id = await _enqueue_job(
        worker_session_factory,
        f"worker-version-conflict-{uuid4()}",
    )
    original_worker = _worker(
        worker_session_factory,
        "worker-original-version",
        classifier=VersionedFakeClassifier(
            version="immutable-v1",
            category=ClassificationCategory.OTHER,
            priority=TicketPriority.MEDIUM,
        ),
    )
    assert await original_worker.run_once() is True

    conflicting_job_id = await _enqueue_reclassification_job(
        worker_session_factory,
        ticket_id,
    )
    conflicting_worker = _worker(
        worker_session_factory,
        "worker-conflicting-version",
        classifier=VersionedFakeClassifier(
            version="immutable-v1",
            category=ClassificationCategory.DELIVERY,
            priority=TicketPriority.HIGH,
            system_prompt="changed-without-version-bump",
        ),
    )
    assert await conflicting_worker.run_once() is True

    async with worker_session_factory() as session:
        conflicting_job = await session.get(ProcessingJob, conflicting_job_id)
        classification_count = await session.scalar(
            select(func.count(Classification.id)).where(Classification.ticket_id == ticket_id)
        )
        version = await session.scalar(
            select(ClassifierVersion).where(
                ClassifierVersion.provider == "version-test",
                ClassifierVersion.model_name == "versioned-fake",
                ClassifierVersion.version == "immutable-v1",
            )
        )

    assert conflicting_job is not None
    assert conflicting_job.status == JobStatus.FAILED
    assert conflicting_job.last_error_code == "classifier_version_conflict"
    assert classification_count == 1
    assert version is not None
    assert version.system_prompt == "prompt-immutable-v1"


async def test_job_fails_after_reaching_max_attempts(
    worker_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id, ticket_id = await _enqueue_job(
        worker_session_factory,
        f"worker-failed-{uuid4()}",
    )
    worker = _worker(
        worker_session_factory,
        "worker-always-fails",
        classifier=FailingFakeClassifier(),
    )

    assert await worker.run_once() is True
    assert await worker.run_once() is True
    assert await worker.run_once() is True
    assert await worker.run_once() is False

    async with worker_session_factory() as session:
        job = await session.get(ProcessingJob, job_id)
        ticket = await session.get(Ticket, ticket_id)
        classification_count = await session.scalar(
            select(func.count(Classification.id)).where(Classification.ticket_id == ticket_id)
        )

    assert job is not None
    assert job.status == JobStatus.FAILED
    assert job.attempt_count == job.max_attempts == 3
    assert job.completed_at is not None
    assert job.locked_at is None
    assert job.locked_by is None
    assert ticket is not None
    assert ticket.status == TicketStatus.FAILED
    assert classification_count == 0
