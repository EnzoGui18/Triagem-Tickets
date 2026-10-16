import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from triagem.core.database import get_session
from triagem.main import app
from triagem.models import Classification, JobType, ProcessingJob, Ticket


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
