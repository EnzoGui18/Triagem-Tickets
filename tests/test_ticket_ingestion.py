import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from triagem.core.database import get_session
from triagem.main import app
from triagem.models import (
    AlertDelivery,
    Classification,
    JobType,
    ProcessingJob,
    Ticket,
    TicketStatus,
)


def _payload(external_id: str) -> dict[str, Any]:
    return {
        "external_id": external_id,
        "cliente": {
            "id": "customer-123",
            "nome": "Cliente Teste",
            "email": "cliente@example.com",
        },
        "assunto": "Pedido não chegou",
        "mensagem": "Meu pedido ainda não foi entregue.",
        "canal": "web",
        "criado_em": "2026-10-16T12:00:00-03:00",
    }


async def _record_counts(database_url: str, external_id: str) -> tuple[int, int, int]:
    test_engine = create_async_engine(database_url)
    try:
        async with AsyncSession(test_engine) as session:
            ticket_count = await session.scalar(
                select(func.count(Ticket.id)).where(Ticket.external_id == external_id)
            )
            job_count = await session.scalar(
                select(func.count(ProcessingJob.id))
                .join(Ticket, ProcessingJob.ticket_id == Ticket.id)
                .where(
                    Ticket.external_id == external_id,
                    ProcessingJob.job_type == JobType.CLASSIFICATION,
                )
            )
            classification_count = await session.scalar(
                select(func.count(Classification.id))
                .join(Ticket, Classification.ticket_id == Ticket.id)
                .where(Ticket.external_id == external_id)
            )
    finally:
        await test_engine.dispose()

    return ticket_count or 0, job_count or 0, classification_count or 0


async def test_valid_ticket_is_accepted_and_enqueued(
    api_client: AsyncClient,
    migrated_database_url: str,
) -> None:
    external_id = f"ticket-valid-{uuid4()}"

    response = await api_client.post("/tickets", json=_payload(external_id))

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.json()["external_id"] == external_id
    assert response.json()["status"] == "received"
    assert "recebido_em" in response.json()
    assert await _record_counts(migrated_database_url, external_id) == (1, 1, 0)

    test_engine = create_async_engine(migrated_database_url)
    try:
        async with AsyncSession(test_engine) as session:
            max_attempts = await session.scalar(
                select(ProcessingJob.max_attempts)
                .join(Ticket, ProcessingJob.ticket_id == Ticket.id)
                .where(Ticket.external_id == external_id)
            )
    finally:
        await test_engine.dispose()
    assert max_attempts == 8


async def test_common_ticket_gets_default_queue_priority(
    api_client: AsyncClient,
    migrated_database_url: str,
) -> None:
    external_id = f"ticket-common-priority-{uuid4()}"
    payload = _payload(external_id)
    payload["assunto"] = "Prazo de entrega"
    payload["mensagem"] = "Quero saber quando meu pedido chegará."

    response = await api_client.post("/tickets", json=payload)

    assert response.status_code == status.HTTP_202_ACCEPTED
    test_engine = create_async_engine(migrated_database_url)
    try:
        async with AsyncSession(test_engine) as session:
            queue_priority = await session.scalar(
                select(ProcessingJob.queue_priority)
                .join(Ticket, ProcessingJob.ticket_id == Ticket.id)
                .where(Ticket.external_id == external_id)
            )
    finally:
        await test_engine.dispose()

    assert queue_priority == 0


async def test_strong_signal_only_elevates_job_order_without_classifying(
    api_client: AsyncClient,
    migrated_database_url: str,
) -> None:
    external_id = f"ticket-elevated-priority-{uuid4()}"
    payload = _payload(external_id)
    payload["assunto"] = "Compras estranhas"
    payload["mensagem"] = "Minha conta foi invadida e não reconheço essas compras."

    with patch("triagem.api.tickets.log_event") as log_event_mock:
        response = await api_client.post("/tickets", json=payload)

    assert response.status_code == status.HTTP_202_ACCEPTED
    test_engine = create_async_engine(migrated_database_url)
    try:
        async with AsyncSession(test_engine) as session:
            ticket = await session.scalar(
                select(Ticket).where(Ticket.external_id == external_id)
            )
            assert ticket is not None
            job = await session.scalar(
                select(ProcessingJob).where(
                    ProcessingJob.ticket_id == ticket.id,
                    ProcessingJob.job_type == JobType.CLASSIFICATION,
                )
            )
            classification_count = await session.scalar(
                select(func.count(Classification.id)).where(
                    Classification.ticket_id == ticket.id
                )
            )
            alert_count = await session.scalar(
                select(func.count(AlertDelivery.id))
                .join(
                    Classification,
                    AlertDelivery.classification_id == Classification.id,
                )
                .where(Classification.ticket_id == ticket.id)
            )
    finally:
        await test_engine.dispose()

    assert ticket.status == TicketStatus.RECEIVED
    assert job is not None
    assert job.queue_priority == 100
    assert job.classification_id is None
    assert classification_count == 0
    assert alert_count == 0
    assert log_event_mock.call_args.kwargs["queue_priority"] == 100
    assert "message" not in log_event_mock.call_args.kwargs


async def test_invalid_ticket_returns_422(api_client: AsyncClient) -> None:
    payload = _payload(f"ticket-invalid-{uuid4()}")
    payload["cliente"]["email"] = "email-invalido"

    response = await api_client.post("/tickets", json=payload)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_repeated_semantically_equal_ticket_returns_existing_ticket(
    api_client: AsyncClient,
    migrated_database_url: str,
) -> None:
    external_id = f"ticket-repeat-{uuid4()}"
    first_payload = _payload(external_id)
    equivalent_payload = _payload(external_id)
    equivalent_payload["criado_em"] = "2026-10-16T15:00:00Z"

    first_response = await api_client.post("/tickets", json=first_payload)
    second_response = await api_client.post("/tickets", json=equivalent_payload)

    assert first_response.status_code == status.HTTP_202_ACCEPTED
    assert second_response.status_code == status.HTTP_202_ACCEPTED
    assert second_response.json() == first_response.json()
    assert await _record_counts(migrated_database_url, external_id) == (1, 1, 0)


async def test_same_external_id_with_different_payload_returns_409(
    api_client: AsyncClient,
    migrated_database_url: str,
) -> None:
    external_id = f"ticket-conflict-{uuid4()}"
    original_payload = _payload(external_id)
    different_payload = _payload(external_id)
    different_payload["mensagem"] = "Este é um conteúdo diferente."

    first_response = await api_client.post("/tickets", json=original_payload)
    conflict_response = await api_client.post("/tickets", json=different_payload)

    assert first_response.status_code == status.HTTP_202_ACCEPTED
    assert conflict_response.status_code == status.HTTP_409_CONFLICT
    assert await _record_counts(migrated_database_url, external_id) == (1, 1, 0)


async def test_concurrent_retries_create_one_ticket_and_one_job(
    api_client: AsyncClient,
    migrated_database_url: str,
) -> None:
    external_id = f"ticket-concurrent-{uuid4()}"
    payload = _payload(external_id)

    responses = await asyncio.gather(
        *(api_client.post("/tickets", json=payload) for _ in range(10))
    )

    assert {response.status_code for response in responses} == {status.HTTP_202_ACCEPTED}
    assert len({response.json()["id"] for response in responses}) == 1
    assert await _record_counts(migrated_database_url, external_id) == (1, 1, 0)


async def test_large_ticket_is_preserved_and_remains_idempotent(
    api_client: AsyncClient,
    migrated_database_url: str,
) -> None:
    external_id = f"ticket-large-{uuid4()}"
    message = "I" + ("x" * 53_998) + "F"
    payload = _payload(external_id)
    payload["mensagem"] = message

    first_response = await api_client.post("/tickets", json=payload)
    second_response = await api_client.post("/tickets", json=payload)

    assert first_response.status_code == status.HTTP_202_ACCEPTED
    assert second_response.status_code == status.HTTP_202_ACCEPTED
    assert second_response.json() == first_response.json()

    test_engine = create_async_engine(migrated_database_url)
    try:
        async with AsyncSession(test_engine) as session:
            ticket = await session.scalar(
                select(Ticket).where(Ticket.external_id == external_id)
            )
            jobs = list(
                (
                    await session.scalars(
                        select(ProcessingJob).where(
                            ProcessingJob.ticket_id == ticket.id,
                            ProcessingJob.job_type == JobType.CLASSIFICATION,
                        )
                    )
                ).all()
            ) if ticket is not None else []
    finally:
        await test_engine.dispose()

    assert ticket is not None
    assert ticket.message == message
    assert len(ticket.message) == 54_000
    assert len(jobs) == 1


async def test_database_failure_does_not_return_success() -> None:
    async def unavailable_session() -> AsyncIterator[AsyncSession]:
        raise OperationalError("BEGIN", {}, RuntimeError("database unavailable"))
        yield  # pragma: no cover

    app.dependency_overrides[get_session] = unavailable_session
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/tickets",
                json=_payload(f"ticket-db-failure-{uuid4()}"),
            )
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
