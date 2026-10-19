from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from triagem.alerting import (
    AlertPayload,
    AlertPermanentError,
    AlertTransientError,
    PlantaoClient,
    build_alert_summary,
)
from triagem.models import ClassificationCategory, Ticket


def _payload() -> AlertPayload:
    return AlertPayload(
        external_id="ticket-123",
        category=ClassificationCategory.DELIVERY,
        priority="urgente",
        summary="Pedido não chegou.",
        detected_at=datetime(2026, 10, 19, 18, tzinfo=UTC),
    )


def test_summary_is_deterministic_normalized_and_limited_to_280_characters() -> None:
    ticket = Ticket(
        external_id=f"summary-{uuid4()}",
        customer_external_id="customer-1",
        customer_name="Cliente",
        customer_email="cliente@example.com",
        subject="  Pedido   urgente ",
        message="sem atualização " * 40,
        channel="web",
        source_created_at=datetime.now(UTC),
        payload_hash="a" * 64,
    )

    first = build_alert_summary(ticket)
    second = build_alert_summary(ticket)

    assert first == second
    assert first.startswith("Pedido urgente: sem atualização")
    assert len(first) == 280
    assert "  " not in first


async def test_client_sends_contract_and_captures_provider_alert_id() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(202, json={"id": "plantao-alert-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await PlantaoClient(
            base_url="http://plantao:8080",
            http_client=http_client,
        ).deliver(_payload())

    assert captured_request is not None
    assert captured_request.url == "http://plantao:8080/alertas"
    assert captured_request.method == "POST"
    assert b'"prioridade":"urgente"' in captured_request.read()
    assert result.provider_alert_id == "plantao-alert-1"


@pytest.mark.parametrize(
    ("status_code", "exception_type", "error_code"),
    [
        (503, AlertTransientError, "plantao_http_503"),
        (422, AlertPermanentError, "plantao_invalid_payload"),
    ],
)
async def test_client_maps_provider_statuses(
    status_code: int,
    exception_type: type[Exception],
    error_code: str,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PlantaoClient(base_url="http://plantao:8080", http_client=http_client)
        with pytest.raises(exception_type, match=error_code):
            await client.deliver(_payload())


@pytest.mark.parametrize("failure", ["timeout", "network"])
async def test_client_maps_transport_failures_as_transient(failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        raise httpx.ConnectError("network", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = PlantaoClient(base_url="http://plantao:8080", http_client=http_client)
        with pytest.raises(AlertTransientError):
            await client.deliver(_payload())
