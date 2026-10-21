from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from triagem.core.config import get_settings
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


@pytest_asyncio.fixture
async def classification_history_dataset(
    migrated_database_url: str,
) -> AsyncIterator[dict[str, Any]]:
    test_engine = create_async_engine(migrated_database_url)
    suffix = uuid4().hex
    external_id = f"history-{suffix}"

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        version_v1 = ClassifierVersion(
            provider="llm-sim",
            model_name="triagem-sim-1",
            version=f"classifier-v1-{suffix}",
            system_prompt="Prompt v1",
            output_schema_version="1",
        )
        version_v2 = ClassifierVersion(
            provider="llm-sim",
            model_name="triagem-sim-1",
            version=f"classifier-v2-{suffix}",
            system_prompt="Prompt v2",
            output_schema_version="1",
        )
        ticket = Ticket(
            external_id=external_id,
            customer_external_id=f"customer-{suffix}",
            customer_name="Cliente Histórico",
            customer_email=f"history-{suffix}@example.com",
            subject="Acompanhar entrega",
            message="Quero saber quando meu pedido chegará.",
            channel="web",
            source_created_at=datetime(2101, 1, 1, 11, tzinfo=UTC),
            received_at=datetime(2101, 1, 1, 12, tzinfo=UTC),
            status=TicketStatus.CLASSIFIED,
            payload_hash="a" * 64,
        )
        session.add_all((version_v1, version_v2, ticket))
        await session.flush()

        first = Classification(
            ticket_id=ticket.id,
            classifier_version_id=version_v1.id,
            category=ClassificationCategory.DELIVERY,
            priority=TicketPriority.HIGH,
            sentiment=Sentiment.NEGATIVE,
            suggested_response="Vamos verificar a entrega.",
            input_tokens=120,
            output_tokens=48,
            input_cost=Decimal("0.00004800"),
            output_cost=Decimal("0.00007680"),
            total_cost=Decimal("0.00012480"),
            created_at=datetime(2101, 1, 1, 12, 1, tzinfo=UTC),
        )
        second = Classification(
            ticket_id=ticket.id,
            classifier_version_id=version_v2.id,
            category=ClassificationCategory.DELIVERY,
            priority=TicketPriority.MEDIUM,
            sentiment=Sentiment.NEUTRAL,
            suggested_response="A entrega está em acompanhamento.",
            input_tokens=31,
            output_tokens=12,
            input_cost=Decimal("0.00001240"),
            output_cost=Decimal("0.00001920"),
            total_cost=Decimal("0.00003160"),
            created_at=datetime(2101, 1, 1, 12, 2, tzinfo=UTC),
        )
        session.add_all((first, second))
        await session.commit()

        ticket_id = ticket.id
        classification_ids = (first.id, second.id)
        version_ids = (version_v1.id, version_v2.id)

    try:
        yield {
            "external_id": external_id,
            "ticket_id": ticket_id,
            "classification_ids": classification_ids,
            "version_ids": version_ids,
            "database_url": migrated_database_url,
        }
    finally:
        async with AsyncSession(test_engine) as session:
            await session.execute(
                delete(ProcessingJob).where(ProcessingJob.ticket_id == ticket_id)
            )
            await session.execute(
                delete(Classification).where(Classification.ticket_id == ticket_id)
            )
            await session.execute(delete(Ticket).where(Ticket.id == ticket_id))
            await session.execute(
                delete(ClassifierVersion).where(ClassifierVersion.id.in_(version_ids))
            )
            await session.commit()
        await test_engine.dispose()


async def test_classification_history_returns_all_versions_in_deterministic_order(
    api_client: AsyncClient,
    classification_history_dataset: dict[str, Any],
) -> None:
    external_id = classification_history_dataset["external_id"]

    response = await api_client.get(f"/tickets/{external_id}/classifications")

    assert response.status_code == 200
    body = response.json()
    assert body["external_id"] == external_id
    assert [item["id"] for item in body["classificacoes"]] == [
        str(classification_id)
        for classification_id in classification_history_dataset["classification_ids"]
    ]
    assert [item["classifier"]["version"] for item in body["classificacoes"]] == [
        f"classifier-v1-{external_id.removeprefix('history-')}",
        f"classifier-v2-{external_id.removeprefix('history-')}",
    ]
    first = body["classificacoes"][0]
    assert first["classifier"]["provider"] == "llm-sim"
    assert first["classifier"]["model"] == "triagem-sim-1"
    assert first["usage"] == {"input_tokens": 120, "output_tokens": 48}
    assert first["custo_usd"] == {
        "entrada": "0.00004800",
        "saida": "0.00007680",
        "total": "0.00012480",
    }


async def test_ticket_detail_exposes_exact_usage_and_cost_for_current_classification(
    api_client: AsyncClient,
    classification_history_dataset: dict[str, Any],
) -> None:
    external_id = classification_history_dataset["external_id"]

    response = await api_client.get(f"/tickets/{external_id}")

    assert response.status_code == 200
    current = response.json()["classificacao_atual"]
    assert current["categoria"] == "entrega"
    assert current["prioridade"] == "media"
    assert current["usage"] == {"input_tokens": 31, "output_tokens": 12}
    assert current["custo_usd"] == {
        "entrada": "0.00001240",
        "saida": "0.00001920",
        "total": "0.00003160",
    }


async def test_reclassify_enqueues_new_job_and_preserves_previous_classifications(
    api_client: AsyncClient,
    classification_history_dataset: dict[str, Any],
) -> None:
    external_id = classification_history_dataset["external_id"]

    response = await api_client.post(f"/tickets/{external_id}/reclassify")

    assert response.status_code == 202
    body = response.json()
    assert body["external_id"] == external_id
    assert body["status"] == "pending"

    test_engine = create_async_engine(classification_history_dataset["database_url"])
    try:
        async with AsyncSession(test_engine) as session:
            job = await session.get(ProcessingJob, UUID(body["job_id"]))
            assert job is not None
            assert job.job_type == JobType.CLASSIFICATION
            assert job.status == JobStatus.PENDING
            assert job.classification_id is None
            assert job.max_attempts == get_settings().classification_max_attempts
            assert job.queue_priority == 0

            classifications_count = await session.scalar(
                select(func.count())
                .select_from(Classification)
                .where(
                    Classification.ticket_id
                    == classification_history_dataset["ticket_id"]
                )
            )
            assert classifications_count == 2
    finally:
        await test_engine.dispose()


async def test_reclassify_returns_409_when_an_active_job_already_exists(
    api_client: AsyncClient,
    classification_history_dataset: dict[str, Any],
) -> None:
    external_id = classification_history_dataset["external_id"]

    first_response = await api_client.post(f"/tickets/{external_id}/reclassify")
    second_response = await api_client.post(f"/tickets/{external_id}/reclassify")

    assert first_response.status_code == 202
    assert second_response.status_code == 409

    test_engine = create_async_engine(classification_history_dataset["database_url"])
    try:
        async with AsyncSession(test_engine) as session:
            active_jobs = await session.scalar(
                select(func.count())
                .select_from(ProcessingJob)
                .where(
                    ProcessingJob.ticket_id == classification_history_dataset["ticket_id"],
                    ProcessingJob.job_type == JobType.CLASSIFICATION,
                    ProcessingJob.status.in_(
                        (
                            JobStatus.PENDING,
                            JobStatus.PROCESSING,
                            JobStatus.RETRY_SCHEDULED,
                        )
                    ),
                )
            )
            assert active_jobs == 1
    finally:
        await test_engine.dispose()


async def test_classification_history_and_reclassify_return_404_for_unknown_ticket(
    api_client: AsyncClient,
) -> None:
    external_id = f"unknown-{uuid4()}"

    history_response = await api_client.get(f"/tickets/{external_id}/classifications")
    reclassify_response = await api_client.post(f"/tickets/{external_id}/reclassify")

    assert history_response.status_code == 404
    assert reclassify_response.status_code == 404
