from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient

from triagem.core import health as health_module
from triagem.core.health import get_readiness_status
from triagem.main import app


def test_liveness(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_when_dependencies_are_available(client: TestClient) -> None:
    async def available() -> dict[str, bool]:
        return {"postgresql": True, "redis": True}

    app.dependency_overrides[get_readiness_status] = available

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"postgresql": "ok", "redis": "ok"},
    }


async def test_redis_unavailable_degrades_readiness_without_blocking_ingestion(
    api_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def postgresql_available() -> None:
        return None

    async def redis_unavailable() -> None:
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(health_module, "check_postgresql", postgresql_available)
    monkeypatch.setattr(health_module, "check_redis", redis_unavailable)
    external_id = f"redis-degraded-{uuid4()}"
    readiness_response = await api_client.get("/health/ready")
    ingestion_response = await api_client.post(
        "/tickets",
        json={
            "external_id": external_id,
            "cliente": {
                "id": "customer-readiness",
                "nome": "Cliente Readiness",
                "email": "readiness@example.com",
            },
            "assunto": "Prazo de entrega",
            "mensagem": "Quero saber quando meu pedido chegará.",
            "canal": "web",
            "criado_em": "2026-10-21T12:00:00-03:00",
        },
    )

    assert readiness_response.status_code == 200
    assert readiness_response.json() == {
        "status": "degraded",
        "checks": {"postgresql": "ok", "redis": "error"},
    }
    assert ingestion_response.status_code == 202
    assert ingestion_response.json()["external_id"] == external_id


@pytest.mark.parametrize("redis_available", [True, False])
def test_postgresql_unavailable_makes_readiness_fail(
    client: TestClient,
    redis_available: bool,
) -> None:
    async def postgresql_unavailable() -> dict[str, bool]:
        return {"postgresql": False, "redis": redis_available}

    app.dependency_overrides[get_readiness_status] = postgresql_unavailable

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "checks": {
            "postgresql": "error",
            "redis": "ok" if redis_available else "error",
        },
    }
