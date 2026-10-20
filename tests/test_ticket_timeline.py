from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from triagem.alerting import PlantaoClient
from triagem.classifiers import LLMClassifier
from triagem.llm.circuit_breaker import AlwaysClosedCircuitBreaker
from triagem.llm.client import LLMClient
from triagem.models import JobType, ProcessingJob, Ticket
from triagem.workers.alert import AlertWorker
from triagem.workers.classification import ClassificationWorker


class ImmediateRateLimiter:
    async def acquire(self) -> None:
        return None


@pytest_asyncio.fixture
async def timeline_session_factory(
    migrated_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    test_engine = create_async_engine(migrated_database_url)
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await test_engine.dispose()


async def test_timeline_represents_llm_and_alert_retries(
    timeline_session_factory: async_sessionmaker[AsyncSession],
    api_client: AsyncClient,
) -> None:
    external_id = f"timeline-{uuid4()}"
    async with timeline_session_factory() as session, session.begin():
        ticket = Ticket(
            external_id=external_id,
            customer_external_id="timeline-customer",
            customer_name="Cliente Timeline",
            customer_email="timeline@example.com",
            subject="Entrega urgente",
            message="O pedido não chegou.",
            channel="web",
            source_created_at=datetime.now(UTC),
            payload_hash="f" * 64,
        )
        session.add(ticket)
        await session.flush()
        ticket_id = ticket.id
        session.add(
            ProcessingJob(
                ticket_id=ticket.id,
                job_type=JobType.CLASSIFICATION,
                queue_priority=100,
                available_at=datetime(2000, 1, 1, tzinfo=UTC),
            )
        )

    llm_responses = iter(
        [
            httpx.Response(
                503,
                json={"error": {"type": "provider_unavailable", "message": "temporary"}},
            ),
            httpx.Response(
                200,
                json={
                    "id": "generation-timeline",
                    "model": "triagem-sim-1",
                    "output_text": json.dumps(
                        {
                            "categoria": "entrega",
                            "prioridade": "urgente",
                            "sentimento": "negativo",
                            "sugestao_resposta": "Vamos priorizar a entrega.",
                        },
                        ensure_ascii=False,
                    ),
                    "usage": {"input_tokens": 20, "output_tokens": 10},
                },
            ),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: next(llm_responses))
    ) as llm_http_client:
        llm_client = LLMClient(
            base_url="http://llm-sim:8080",
            api_key="test-key",
            rate_limiter=ImmediateRateLimiter(),
            circuit_breaker=AlwaysClosedCircuitBreaker(),
            max_concurrency=1,
            http_client=llm_http_client,
        )
        classification_worker = ClassificationWorker(
            timeline_session_factory,
            LLMClassifier(llm_client),
            worker_id=f"timeline-classification-{uuid4()}",
            lease_duration=timedelta(seconds=30),
            retry_delay=timedelta(0),
            max_retry_delay=timedelta(seconds=30),
            jitter_source=lambda _minimum, _maximum: 0.0,
        )
        assert await classification_worker.run_once() is True
        async with timeline_session_factory() as session, session.begin():
            await session.execute(
                update(ProcessingJob)
                .where(
                    ProcessingJob.ticket_id == ticket_id,
                    ProcessingJob.job_type == JobType.CLASSIFICATION,
                )
                .values(available_at=datetime(2000, 1, 1, tzinfo=UTC))
            )
        assert await classification_worker.run_once() is True

    alert_responses = iter(
        [
            httpx.Response(503),
            httpx.Response(202, json={"id": "alert-timeline"}),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: next(alert_responses))
    ) as alert_http_client:
        alert_worker = AlertWorker(
            timeline_session_factory,
            PlantaoClient(
                base_url="http://plantao:8080",
                http_client=alert_http_client,
            ),
            worker_id=f"timeline-alert-{uuid4()}",
            lease_duration=timedelta(seconds=30),
            retry_delay=timedelta(0),
            max_retry_delay=timedelta(seconds=30),
            jitter_source=lambda _minimum, _maximum: 0.0,
        )
        assert await alert_worker.run_once() is True
        async with timeline_session_factory() as session, session.begin():
            alert_job_id = await session.scalar(
                select(ProcessingJob.id).where(
                    ProcessingJob.ticket_id == ticket_id,
                    ProcessingJob.job_type == JobType.ALERT_DELIVERY,
                )
            )
            assert alert_job_id is not None
            await session.execute(
                update(ProcessingJob)
                .where(ProcessingJob.id == alert_job_id)
                .values(available_at=datetime(2000, 1, 1, tzinfo=UTC))
            )
        assert await alert_worker.run_once() is True

    response = await api_client.get(f"/tickets/{external_id}/timeline")

    assert response.status_code == 200
    body = response.json()
    assert body["external_id"] == external_id
    events = body["eventos"]
    assert [event["tipo"] for event in events] == [
        "ticket_received",
        "classification_queued",
        "classification_started",
        "classification_retry",
        "classification_started",
        "classification_completed",
        "alert_created",
        "alert_attempt",
        "alert_retry",
        "alert_attempt",
        "alert_delivered",
    ]
    assert events[1]["status"] == "pending"
    assert events[3]["codigo_erro"] == "llm_http_503"
    assert events[5]["prioridade"] == "urgente"
    assert events[5]["provider_request_id"] == "generation-timeline"
    assert events[8]["codigo_erro"] == "plantao_http_503"
    assert events[8]["http_status"] == 503
    assert events[10]["provider_request_id"] == "alert-timeline"


async def test_timeline_unknown_ticket_returns_404(api_client: AsyncClient) -> None:
    response = await api_client.get(f"/tickets/unknown-{uuid4()}/timeline")

    assert response.status_code == 404
