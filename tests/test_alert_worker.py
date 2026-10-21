from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from triagem.alerting import PlantaoClient, build_alert_payload
from triagem.classifiers import ClassificationResult, FakeClassifier
from triagem.models import (
    AlertDelivery,
    AlertDeliveryStatus,
    Classification,
    ClassificationCategory,
    ClassifierVersion,
    JobStatus,
    JobType,
    ProcessingJob,
    Sentiment,
    Ticket,
    TicketPriority,
)
from triagem.workers.alert import AlertWorker
from triagem.workers.classification import ClassificationWorker


@pytest_asyncio.fixture
async def alert_session_factory(
    migrated_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    test_engine = create_async_engine(migrated_database_url)
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(
            delete(ProcessingJob).where(ProcessingJob.job_type == JobType.ALERT_DELIVERY)
        )
        await session.execute(delete(AlertDelivery))
    try:
        yield factory
    finally:
        await test_engine.dispose()


class UrgentFakeClassifier(FakeClassifier):
    async def classify(self, ticket: Ticket) -> ClassificationResult:
        del ticket
        return ClassificationResult(
            category=ClassificationCategory.DELIVERY,
            priority=TicketPriority.URGENT,
            sentiment=Sentiment.NEGATIVE,
            suggested_response="Vamos priorizar a entrega.",
            input_tokens=10,
            output_tokens=5,
        )


async def _seed_classification_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    long_message: bool = False,
) -> tuple[UUID, UUID]:
    async with session_factory() as session, session.begin():
        ticket = Ticket(
            external_id=f"urgent-classification-{uuid4()}",
            customer_external_id="customer-urgent",
            customer_name="Cliente Urgente",
            customer_email="urgent@example.com",
            subject="Entrega urgente",
            message=("Pedido não chegou e preciso de ajuda. " * 20 if long_message else "Atraso."),
            channel="web",
            source_created_at=datetime.now(UTC),
            payload_hash="b" * 64,
        )
        session.add(ticket)
        await session.flush()
        job = ProcessingJob(
            ticket_id=ticket.id,
            job_type=JobType.CLASSIFICATION,
            available_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        session.add(job)
        await session.flush()
        return job.id, ticket.id


async def _seed_alert_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    max_attempts: int = 3,
    received_at: datetime | None = None,
) -> tuple[UUID, UUID, UUID]:
    async with session_factory() as session, session.begin():
        detected_at = received_at or datetime.now(UTC)
        identifier = uuid4()
        ticket = Ticket(
            external_id=f"alert-worker-{identifier}",
            customer_external_id=f"customer-{identifier}",
            customer_name="Cliente Alertas",
            customer_email="alertas@example.com",
            subject="Pedido urgente",
            message="O pedido está muito atrasado.",
            channel="web",
            source_created_at=detected_at,
            received_at=detected_at,
            payload_hash="c" * 64,
        )
        version = ClassifierVersion(
            provider="alert-tests",
            model_name="fake",
            version=str(identifier),
            system_prompt="Classifique.",
            output_schema_version="1",
        )
        session.add_all([ticket, version])
        await session.flush()
        classification = Classification(
            ticket_id=ticket.id,
            classifier_version_id=version.id,
            category=ClassificationCategory.DELIVERY,
            priority=TicketPriority.URGENT,
            sentiment=Sentiment.NEGATIVE,
            suggested_response="Vamos verificar.",
            created_at=detected_at,
        )
        session.add(classification)
        await session.flush()
        payload = build_alert_payload(
            ticket,
            category=classification.category,
            detected_at=detected_at,
        )
        delivery = AlertDelivery(
            classification_id=classification.id,
            attempt_number=1,
            status=AlertDeliveryStatus.PENDING,
            request_payload=payload.model_dump(mode="json", by_alias=True),
        )
        job = ProcessingJob(
            ticket_id=ticket.id,
            classification_id=classification.id,
            job_type=JobType.ALERT_DELIVERY,
            queue_priority=100,
            max_attempts=max_attempts,
            available_at=detected_at - timedelta(seconds=1),
        )
        session.add_all([delivery, job])
        await session.flush()
        return job.id, ticket.id, classification.id


def _worker(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    worker_id: str,
) -> AlertWorker:
    return AlertWorker(
        session_factory,
        PlantaoClient(base_url="http://plantao:8080", http_client=http_client),
        worker_id=worker_id,
        lease_duration=timedelta(seconds=30),
        retry_delay=timedelta(0),
        max_retry_delay=timedelta(seconds=30),
        jitter_source=lambda _minimum, _maximum: 0.0,
        poll_interval_seconds=0.01,
    )


async def test_urgent_classification_creates_alert_and_job_in_same_completion(
    alert_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    classification_job_id, ticket_id = await _seed_classification_job(
        alert_session_factory,
        long_message=True,
    )
    worker = ClassificationWorker(
        alert_session_factory,
        UrgentFakeClassifier(),
        worker_id=f"classification-alert-{uuid4()}",
        lease_duration=timedelta(seconds=30),
        retry_delay=timedelta(0),
    )

    assert await worker.run_once() is True

    async with alert_session_factory() as session:
        classification_job = await session.get(ProcessingJob, classification_job_id)
        classification = await session.scalar(
            select(Classification).where(Classification.ticket_id == ticket_id)
        )
        assert classification is not None
        alert_job = await session.scalar(
            select(ProcessingJob).where(
                ProcessingJob.ticket_id == ticket_id,
                ProcessingJob.job_type == JobType.ALERT_DELIVERY,
            )
        )
        delivery = await session.scalar(
            select(AlertDelivery).where(
                AlertDelivery.classification_id == classification.id
            )
        )

    assert classification_job is not None
    assert classification_job.status == JobStatus.COMPLETED
    assert classification.priority == TicketPriority.URGENT
    assert alert_job is not None
    assert alert_job.status == JobStatus.PENDING
    assert alert_job.classification_id == classification.id
    assert alert_job.max_attempts == 8
    assert delivery is not None
    assert delivery.status == AlertDeliveryStatus.PENDING
    assert delivery.attempt_number == 1
    assert delivery.request_payload["prioridade"] == "urgente"
    assert len(str(delivery.request_payload["resumo"])) <= 280
    assert datetime.fromisoformat(str(delivery.request_payload["detectado_em"])) == (
        classification.created_at
    )


async def test_non_urgent_classification_does_not_create_alert(
    alert_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, ticket_id = await _seed_classification_job(alert_session_factory)
    worker = ClassificationWorker(
        alert_session_factory,
        FakeClassifier(),
        worker_id=f"classification-no-alert-{uuid4()}",
        lease_duration=timedelta(seconds=30),
        retry_delay=timedelta(0),
    )

    assert await worker.run_once() is True

    async with alert_session_factory() as session:
        alert_job_count = await session.scalar(
            select(func.count(ProcessingJob.id)).where(
                ProcessingJob.ticket_id == ticket_id,
                ProcessingJob.job_type == JobType.ALERT_DELIVERY,
            )
        )
        alert_count = await session.scalar(
            select(func.count(AlertDelivery.id))
            .join(Classification)
            .where(Classification.ticket_id == ticket_id)
        )

    assert alert_job_count == 0
    assert alert_count == 0


async def test_202_marks_delivery_and_job_completed(
    alert_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id, _, classification_id = await _seed_alert_job(alert_session_factory)
    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(202, json={"id": "provider-alert-123"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        worker = _worker(alert_session_factory, http_client, f"alert-success-{uuid4()}")
        assert await worker.run_once() is True

    async with alert_session_factory() as session:
        job = await session.get(ProcessingJob, job_id)
        delivery = await session.scalar(
            select(AlertDelivery).where(AlertDelivery.classification_id == classification_id)
        )

    assert captured_payload["external_id"]
    assert captured_payload["categoria"] == "entrega"
    assert captured_payload["prioridade"] == "urgente"
    assert len(str(captured_payload["resumo"])) <= 280
    assert captured_payload["detectado_em"]
    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.attempt_count == 1
    assert delivery is not None
    assert delivery.status == AlertDeliveryStatus.DELIVERED
    assert delivery.http_status_code == 202
    assert delivery.provider_request_id == "provider-alert-123"
    assert delivery.attempted_at is not None
    assert delivery.delivered_at is not None


@pytest.mark.parametrize(
    ("elapsed_seconds", "expected_sla_violations"),
    [(42.0, 0), (65.0, 1)],
)
async def test_202_observes_urgent_alert_end_to_end_sla_with_controlled_timestamps(
    alert_session_factory: async_sessionmaker[AsyncSession],
    elapsed_seconds: float,
    expected_sla_violations: int,
) -> None:
    received_at = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    delivered_at = received_at + timedelta(seconds=elapsed_seconds)
    _, _, classification_id = await _seed_alert_job(
        alert_session_factory,
        received_at=received_at,
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(202))
    ) as http_client:
        worker = _worker(alert_session_factory, http_client, f"alert-sla-{uuid4()}")

        async def controlled_database_now(_: AsyncSession) -> datetime:
            return delivered_at

        with (
            patch.object(worker, "_database_now", controlled_database_now),
            patch("triagem.workers.alert.URGENT_ALERT_END_TO_END") as histogram,
            patch("triagem.workers.alert.URGENT_ALERT_SLA_VIOLATIONS") as violations,
        ):
            assert await worker.run_once() is True

    async with alert_session_factory() as session:
        delivery = await session.scalar(
            select(AlertDelivery).where(
                AlertDelivery.classification_id == classification_id
            )
        )

    assert delivery is not None
    assert delivery.delivered_at == delivered_at
    histogram.observe.assert_called_once_with(elapsed_seconds)
    assert violations.inc.call_count == expected_sla_violations


async def test_503_retries_then_delivers_without_reclassifying(
    alert_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id, ticket_id, classification_id = await _seed_alert_job(alert_session_factory)
    responses = [503, 202]

    def handler(_: httpx.Request) -> httpx.Response:
        status_code = responses.pop(0)
        return httpx.Response(status_code, json={"id": "provider-alert-retry"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        worker = _worker(alert_session_factory, http_client, f"alert-retry-{uuid4()}")
        assert await worker.run_once() is True
        assert await worker.run_once() is True

    async with alert_session_factory() as session:
        job = await session.get(ProcessingJob, job_id)
        deliveries = list(
            (
                await session.scalars(
                    select(AlertDelivery)
                    .where(AlertDelivery.classification_id == classification_id)
                    .order_by(AlertDelivery.attempt_number)
                )
            ).all()
        )
        classification_count = await session.scalar(
            select(func.count(Classification.id)).where(Classification.ticket_id == ticket_id)
        )
        first_attempt_at, last_attempt_at = (
            await session.execute(
                select(
                    func.min(AlertDelivery.attempted_at),
                    func.max(AlertDelivery.attempted_at),
                ).where(AlertDelivery.classification_id == classification_id)
            )
        ).one()

    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.attempt_count == 2
    assert [delivery.status for delivery in deliveries] == [
        AlertDeliveryStatus.TRANSIENT_FAILURE,
        AlertDeliveryStatus.DELIVERED,
    ]
    assert deliveries[0].http_status_code == 503
    assert deliveries[0].error_code == "plantao_http_503"
    assert deliveries[1].provider_request_id == "provider-alert-retry"
    assert classification_count == 1
    assert first_attempt_at is not None
    assert last_attempt_at is not None
    assert first_attempt_at <= last_attempt_at


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [("timeout", "plantao_timeout"), ("network", "plantao_network_failure")],
)
async def test_transport_failure_is_scheduled_for_retry(
    alert_session_factory: async_sessionmaker[AsyncSession],
    failure: str,
    expected_code: str,
) -> None:
    job_id, _, classification_id = await _seed_alert_job(alert_session_factory)

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        raise httpx.ConnectError("network", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        worker = _worker(alert_session_factory, http_client, f"alert-{failure}-{uuid4()}")
        assert await worker.run_once() is True

    async with alert_session_factory() as session:
        job = await session.get(ProcessingJob, job_id)
        delivery = await session.scalar(
            select(AlertDelivery).where(AlertDelivery.classification_id == classification_id)
        )

    assert job is not None
    assert job.status == JobStatus.RETRY_SCHEDULED
    assert job.last_error_code == expected_code
    assert delivery is not None
    assert delivery.status == AlertDeliveryStatus.TRANSIENT_FAILURE


async def test_422_is_a_permanent_failure(
    alert_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id, _, classification_id = await _seed_alert_job(alert_session_factory)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(422)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        worker = _worker(alert_session_factory, http_client, f"alert-invalid-{uuid4()}")
        assert await worker.run_once() is True
        assert await worker.run_once() is False

    async with alert_session_factory() as session:
        job = await session.get(ProcessingJob, job_id)
        delivery = await session.scalar(
            select(AlertDelivery).where(AlertDelivery.classification_id == classification_id)
        )

    assert job is not None
    assert job.status == JobStatus.FAILED
    assert job.attempt_count == 1
    assert job.last_error_code == "plantao_invalid_payload"
    assert delivery is not None
    assert delivery.status == AlertDeliveryStatus.PERMANENT_FAILURE
    assert delivery.http_status_code == 422


async def test_two_workers_do_not_deliver_the_same_attempt(
    alert_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id, _, _ = await _seed_alert_job(alert_session_factory)
    request_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        first = _worker(alert_session_factory, http_client, f"alert-concurrent-a-{uuid4()}")
        second = _worker(alert_session_factory, http_client, f"alert-concurrent-b-{uuid4()}")
        results = await asyncio.gather(first.run_once(), second.run_once())

    async with alert_session_factory() as session:
        job = await session.get(ProcessingJob, job_id)

    assert results.count(True) == 1
    assert request_count == 1
    assert job is not None
    assert job.status == JobStatus.COMPLETED


async def test_expired_lease_is_retried_as_a_new_attempt(
    alert_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    job_id, _, classification_id = await _seed_alert_job(alert_session_factory)

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(202))) as c:
        crashed_worker = _worker(alert_session_factory, c, f"alert-crashed-{uuid4()}")
        reserved = await crashed_worker.reserve_next_job()
        assert reserved is not None

    async with alert_session_factory() as session, session.begin():
        await session.execute(
            update(ProcessingJob)
            .where(ProcessingJob.id == job_id)
            .values(locked_at=datetime.now(UTC) - timedelta(minutes=2))
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(202))) as c:
        recovery_worker = _worker(alert_session_factory, c, f"alert-recovery-{uuid4()}")
        assert await recovery_worker.run_once() is True

    async with alert_session_factory() as session:
        job = await session.get(ProcessingJob, job_id)
        deliveries = list(
            (
                await session.scalars(
                    select(AlertDelivery)
                    .where(AlertDelivery.classification_id == classification_id)
                    .order_by(AlertDelivery.attempt_number)
                )
            ).all()
        )

    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.attempt_count == 2
    assert [delivery.status for delivery in deliveries] == [
        AlertDeliveryStatus.TRANSIENT_FAILURE,
        AlertDeliveryStatus.DELIVERED,
    ]
    assert deliveries[0].error_code == "alert_lease_expired"
