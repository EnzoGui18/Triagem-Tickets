from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from triagem.models import (
    Classification,
    ClassificationCategory,
    ClassifierVersion,
    JobType,
    ProcessingJob,
    Sentiment,
    Ticket,
    TicketPriority,
    TicketStatus,
)


@pytest_asyncio.fixture
async def ticket_query_dataset(
    migrated_database_url: str,
) -> AsyncIterator[dict[str, Any]]:
    test_engine = create_async_engine(migrated_database_url)
    prefix = f"query-{uuid4()}"
    version_id = None
    ticket_ids = []

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        version = ClassifierVersion(
            provider="query-tests",
            model_name="controlled-model",
            version=prefix,
            system_prompt="Classifique o ticket.",
            output_schema_version="1",
        )
        session.add(version)

        ticket_specs = (
            ("a", datetime(2100, 1, 1, 12, tzinfo=UTC), TicketStatus.CLASSIFIED),
            ("b", datetime(2100, 1, 15, 12, tzinfo=UTC), TicketStatus.CLASSIFIED),
            ("c", datetime(2100, 2, 1, 12, tzinfo=UTC), TicketStatus.CLASSIFIED),
            ("d", datetime(2100, 3, 1, 12, tzinfo=UTC), TicketStatus.RECEIVED),
        )
        tickets: dict[str, Ticket] = {}
        for suffix, received_at, ticket_status in ticket_specs:
            ticket = Ticket(
                external_id=f"{prefix}-{suffix}",
                customer_external_id=f"customer-{suffix}",
                customer_name=f"Cliente {suffix.upper()}",
                customer_email=f"cliente-{suffix}@example.com",
                subject=f"Assunto {suffix.upper()}",
                message=f"Mensagem controlada {suffix.upper()}",
                channel="web",
                source_created_at=received_at,
                received_at=received_at,
                status=ticket_status,
                payload_hash=suffix * 64,
            )
            tickets[suffix] = ticket
            session.add(ticket)

        await session.flush()
        version_id = version.id
        ticket_ids = [ticket.id for ticket in tickets.values()]

        session.add_all(
            [
                Classification(
                    ticket_id=tickets["a"].id,
                    classifier_version_id=version.id,
                    category=ClassificationCategory.PRODUCT,
                    priority=TicketPriority.LOW,
                    sentiment=Sentiment.NEUTRAL,
                    suggested_response="Classificação antiga.",
                    created_at=datetime(2100, 1, 1, 13, tzinfo=UTC),
                ),
                Classification(
                    ticket_id=tickets["a"].id,
                    classifier_version_id=version.id,
                    category=ClassificationCategory.PAYMENT,
                    priority=TicketPriority.HIGH,
                    sentiment=Sentiment.NEGATIVE,
                    suggested_response="Classificação atual A.",
                    created_at=datetime(2100, 1, 1, 14, tzinfo=UTC),
                ),
                Classification(
                    ticket_id=tickets["b"].id,
                    classifier_version_id=version.id,
                    category=ClassificationCategory.DELIVERY,
                    priority=TicketPriority.URGENT,
                    sentiment=Sentiment.NEGATIVE,
                    suggested_response="Classificação atual B.",
                    created_at=datetime(2100, 1, 15, 14, tzinfo=UTC),
                ),
                Classification(
                    ticket_id=tickets["c"].id,
                    classifier_version_id=version.id,
                    category=ClassificationCategory.PAYMENT,
                    priority=TicketPriority.URGENT,
                    sentiment=Sentiment.NEUTRAL,
                    suggested_response="Classificação atual C.",
                    created_at=datetime(2100, 2, 1, 14, tzinfo=UTC),
                ),
                ProcessingJob(
                    ticket_id=tickets["d"].id,
                    job_type=JobType.CLASSIFICATION,
                    queue_priority=0,
                ),
            ]
        )
        await session.commit()

    try:
        yield {
            "prefix": prefix,
            "external_ids": {
                suffix: f"{prefix}-{suffix}" for suffix in ("a", "b", "c", "d")
            },
        }
    finally:
        async with AsyncSession(test_engine) as session:
            await session.execute(
                delete(ProcessingJob).where(ProcessingJob.ticket_id.in_(ticket_ids))
            )
            await session.execute(
                delete(Classification).where(Classification.ticket_id.in_(ticket_ids))
            )
            await session.execute(delete(Ticket).where(Ticket.id.in_(ticket_ids)))
            if version_id is not None:
                await session.execute(
                    delete(ClassifierVersion).where(ClassifierVersion.id == version_id)
                )
            await session.commit()
        await test_engine.dispose()


async def test_get_ticket_returns_current_classification(
    api_client: AsyncClient,
    ticket_query_dataset: dict[str, Any],
) -> None:
    external_id = ticket_query_dataset["external_ids"]["a"]

    response = await api_client.get(f"/tickets/{external_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["external_id"] == external_id
    assert body["cliente"]["id"] == "customer-a"
    assert body["classificacao_atual"]["categoria"] == "pagamento"
    assert body["classificacao_atual"]["prioridade"] == "alta"
    assert body["processamento"] is None


async def test_get_ticket_returns_pending_processing_state(
    api_client: AsyncClient,
    ticket_query_dataset: dict[str, Any],
) -> None:
    external_id = ticket_query_dataset["external_ids"]["d"]

    response = await api_client.get(f"/tickets/{external_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "received"
    assert body["classificacao_atual"] is None
    assert body["processamento"]["status"] == "pending"
    assert body["processamento"]["tentativas"] == 0


async def test_get_unknown_ticket_returns_404(api_client: AsyncClient) -> None:
    response = await api_client.get(f"/tickets/not-found-{uuid4()}")

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("params", "expected_suffixes"),
    [
        ({"categoria": "pagamento"}, {"a", "c"}),
        ({"prioridade": "urgente"}, {"b", "c"}),
        (
            {"inicio": "2100-01-10T00:00:00Z", "fim": "2100-02-10T00:00:00Z"},
            {"b", "c"},
        ),
        ({"categoria": "pagamento", "prioridade": "urgente"}, {"c"}),
        (
            {
                "categoria": "pagamento",
                "inicio": "2100-01-01T00:00:00Z",
                "fim": "2100-01-31T23:59:59Z",
            },
            {"a"},
        ),
        (
            {
                "categoria": "pagamento",
                "prioridade": "urgente",
                "inicio": "2100-02-01T00:00:00Z",
                "fim": "2100-02-28T23:59:59Z",
            },
            {"c"},
        ),
    ],
    ids=(
        "categoria",
        "prioridade",
        "periodo",
        "categoria-prioridade",
        "categoria-periodo",
        "todos",
    ),
)
async def test_list_tickets_applies_filter_combinations(
    api_client: AsyncClient,
    ticket_query_dataset: dict[str, Any],
    params: dict[str, str],
    expected_suffixes: set[str],
) -> None:
    response = await api_client.get(
        "/tickets",
        params={**params, "page_size": "100"},
    )

    assert response.status_code == 200
    prefix = ticket_query_dataset["prefix"]
    controlled_ids = {
        item["external_id"]
        for item in response.json()["items"]
        if item["external_id"].startswith(prefix)
    }
    assert controlled_ids == {f"{prefix}-{suffix}" for suffix in expected_suffixes}


async def test_list_tickets_paginates_in_postgresql(
    api_client: AsyncClient,
    ticket_query_dataset: dict[str, Any],
) -> None:
    response = await api_client.get(
        "/tickets",
        params={
            "inicio": "2100-01-01T00:00:00Z",
            "fim": "2100-03-31T23:59:59Z",
            "page": "2",
            "page_size": "2",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 4
    assert body["page"] == 2
    assert body["page_size"] == 2
    assert body["total_pages"] == 2
    assert [item["external_id"] for item in body["items"]] == [
        ticket_query_dataset["external_ids"]["b"],
        ticket_query_dataset["external_ids"]["a"],
    ]


async def test_list_tickets_rejects_invalid_period(api_client: AsyncClient) -> None:
    response = await api_client.get(
        "/tickets",
        params={
            "inicio": "2100-02-01T00:00:00Z",
            "fim": "2100-01-01T00:00:00Z",
        },
    )

    assert response.status_code == 422
