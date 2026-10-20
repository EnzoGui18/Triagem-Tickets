import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from triagem.classifiers import ClassificationResult, FakeClassifier, LLMClassifier
from triagem.llm.circuit_breaker import AlwaysClosedCircuitBreaker
from triagem.llm.client import LLMClient
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


class ImmediateRateLimiter:
    async def acquire(self) -> None:
        return None


@pytest_asyncio.fixture
async def real_worker_session_factory(
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


async def _enqueue_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    max_attempts: int = 3,
    queue_priority: int = 10,
) -> tuple[UUID, UUID]:
    async with session_factory() as session, session.begin():
        now = await session.scalar(select(func.clock_timestamp()))
        if now is None:  # pragma: no cover - PostgreSQL always returns a value
            raise RuntimeError("database clock is unavailable")
        identifier = uuid4()
        ticket = Ticket(
            external_id=f"real-worker-{identifier}",
            customer_external_id=f"customer-{identifier}",
            customer_name="Cliente não enviado ao LLM",
            customer_email="private@example.com",
            subject="Pedido atrasado",
            message="Meu pedido ainda não chegou.",
            channel="web",
            source_created_at=datetime.now(UTC),
            payload_hash="d" * 64,
        )
        session.add(ticket)
        await session.flush()
        job = ProcessingJob(
            ticket_id=ticket.id,
            job_type=JobType.CLASSIFICATION,
            queue_priority=queue_priority,
            max_attempts=max_attempts,
            available_at=now - timedelta(minutes=1),
        )
        session.add(job)
        await session.flush()
        return job.id, ticket.id


def _valid_output() -> str:
    return json.dumps(
        {
            "categoria": "entrega",
            "prioridade": "alta",
            "sentimento": "negativo",
            "sugestao_resposta": "Vamos verificar a entrega do seu pedido.",
        },
        ensure_ascii=False,
    )


def _provider_success(*, output_text: str, generation_id: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": generation_id,
            "model": "triagem-sim-1",
            "output_text": output_text,
            "usage": {"input_tokens": 31, "output_tokens": 12},
        },
    )


def _real_worker(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    *,
    worker_id: str,
) -> ClassificationWorker:
    client = LLMClient(
        base_url="http://llm-sim:8080",
        api_key="test-key",
        rate_limiter=ImmediateRateLimiter(),
        circuit_breaker=AlwaysClosedCircuitBreaker(),
        max_concurrency=2,
        http_client=http_client,
    )
    return ClassificationWorker(
        session_factory,
        LLMClassifier(client),
        worker_id=worker_id,
        lease_duration=timedelta(seconds=30),
        retry_delay=timedelta(0),
        max_retry_delay=timedelta(seconds=30),
        jitter_source=lambda _minimum, _maximum: 0.0,
        poll_interval_seconds=0.01,
    )


@pytest.mark.parametrize(
    "steps",
    [
        pytest.param(["500", "500", "success"], id="500-500-200"),
        pytest.param(["503", "success"], id="503-200"),
        pytest.param(["invalid-output", "success"], id="invalid-output-valid-output"),
    ],
)
async def test_real_worker_retries_then_persists_provider_result(
    steps: list[str],
    real_worker_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id, ticket_id = await _enqueue_job(real_worker_session_factory)
    remaining_steps = list(steps)
    valid_output = _valid_output()

    def handler(_: httpx.Request) -> httpx.Response:
        step = remaining_steps.pop(0)
        if step in {"500", "503"}:
            return httpx.Response(
                int(step),
                json={"error": {"type": "provider_unavailable", "message": "temporary"}},
            )
        if step == "invalid-output":
            return _provider_success(output_text="resposta sem JSON", generation_id="gen-invalid")
        return _provider_success(output_text=valid_output, generation_id="gen-success")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        worker = _real_worker(
            real_worker_session_factory,
            http_client,
            worker_id=f"worker-{'-'.join(steps)}-{uuid4()}",
        )
        for _ in steps:
            assert await worker.run_once() is True
        assert await worker.run_once() is False

    async with real_worker_session_factory() as session:
        job = await session.get(ProcessingJob, job_id)
        ticket = await session.get(Ticket, ticket_id)
        classification = await session.scalar(
            select(Classification).where(Classification.ticket_id == ticket_id)
        )
        version = await session.scalar(
            select(ClassifierVersion).where(ClassifierVersion.provider == "llm-sim")
        )

    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.attempt_count == len(steps)
    assert job.completed_at is not None
    assert ticket is not None
    assert ticket.status == TicketStatus.CLASSIFIED
    assert classification is not None
    assert classification.category == ClassificationCategory.DELIVERY
    assert classification.priority == TicketPriority.HIGH
    assert classification.sentiment == Sentiment.NEGATIVE
    assert classification.suggested_response == "Vamos verificar a entrega do seu pedido."
    assert classification.provider_request_id == "gen-success"
    assert classification.provider_model == "triagem-sim-1"
    assert classification.raw_output == valid_output
    assert classification.input_tokens == 31
    assert classification.output_tokens == 12
    assert classification.input_cost == Decimal("0.00001240")
    assert classification.output_cost == Decimal("0.00001920")
    assert classification.total_cost == Decimal("0.00003160")
    assert classification.provider_latency_ms is not None
    assert classification.provider_latency_ms >= 0
    assert classification.created_at is not None
    assert version is not None
    assert version.model_name == "triagem-sim-1"
    assert version.version == "classifier-v1"


async def test_rate_limit_retry_after_is_persisted_without_sleeping(
    real_worker_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id, _ = await _enqueue_job(real_worker_session_factory)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"type": "rate_limit", "message": "slow down"}},
            headers={"Retry-After": "2"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        worker = _real_worker(
            real_worker_session_factory,
            http_client,
            worker_id=f"worker-rate-limit-{uuid4()}",
        )
        assert await worker.run_once() is True

    async with real_worker_session_factory() as session:
        job = await session.get(ProcessingJob, job_id)

    assert job is not None
    assert job.status == JobStatus.RETRY_SCHEDULED
    assert job.attempt_count == 1
    assert job.last_error_code == "llm_rate_limited"
    assert (job.available_at - job.updated_at).total_seconds() >= 2


@pytest.mark.parametrize(
    ("status_code", "error_type", "expected_code"),
    [
        (401, "unauthorized", "llm_authentication_failed"),
        (400, "context_length_exceeded", "llm_context_length_exceeded"),
    ],
)
async def test_permanent_provider_failures_are_not_retried(
    status_code: int,
    error_type: str,
    expected_code: str,
    real_worker_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id, ticket_id = await _enqueue_job(real_worker_session_factory)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"error": {"type": error_type, "message": "permanent"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        worker = _real_worker(
            real_worker_session_factory,
            http_client,
            worker_id=f"worker-permanent-{status_code}-{uuid4()}",
        )
        assert await worker.run_once() is True
        assert await worker.run_once() is False

    async with real_worker_session_factory() as session:
        job = await session.get(ProcessingJob, job_id)
        ticket = await session.get(Ticket, ticket_id)

    assert job is not None
    assert job.status == JobStatus.FAILED
    assert job.attempt_count == 1
    assert job.last_error_code == expected_code
    assert ticket is not None
    assert ticket.status == TicketStatus.FAILED


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [("timeout", "llm_timeout"), ("network", "llm_network_failure")],
)
async def test_transport_failures_are_scheduled_for_retry(
    failure: str,
    expected_code: str,
    real_worker_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id, _ = await _enqueue_job(real_worker_session_factory)

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        raise httpx.ConnectError("network", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        worker = _real_worker(
            real_worker_session_factory,
            http_client,
            worker_id=f"worker-transport-{failure}-{uuid4()}",
        )
        assert await worker.run_once() is True

    async with real_worker_session_factory() as session:
        job = await session.get(ProcessingJob, job_id)

    assert job is not None
    assert job.status == JobStatus.RETRY_SCHEDULED
    assert job.last_error_code == expected_code


class AlwaysFailingClassifier(FakeClassifier):
    async def classify(self, ticket: Ticket) -> ClassificationResult:
        del ticket
        raise RuntimeError("simulated failure")


async def test_retry_uses_exponential_backoff_with_jitter(
    real_worker_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id, _ = await _enqueue_job(real_worker_session_factory, queue_priority=100)
    worker = ClassificationWorker(
        real_worker_session_factory,
        AlwaysFailingClassifier(),
        worker_id=f"worker-backoff-{uuid4()}",
        lease_duration=timedelta(seconds=30),
        retry_delay=timedelta(seconds=10),
        max_retry_delay=timedelta(seconds=60),
        jitter_source=lambda _minimum, maximum: maximum,
        poll_interval_seconds=0.01,
    )

    assert await worker.run_once() is True
    async with real_worker_session_factory() as session:
        first_retry = await session.get(ProcessingJob, job_id)
        assert first_retry is not None
        first_delay = (first_retry.available_at - first_retry.updated_at).total_seconds()
    assert first_delay == pytest.approx(12.5)

    async with real_worker_session_factory() as session, session.begin():
        await session.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id)
            .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    assert await worker.run_once() is True
    async with real_worker_session_factory() as session:
        second_retry = await session.get(ProcessingJob, job_id)
        assert second_retry is not None
        second_delay = (second_retry.available_at - second_retry.updated_at).total_seconds()
    assert second_delay == pytest.approx(25.0)
