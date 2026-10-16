import pytest
from fastapi.testclient import TestClient

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


@pytest.mark.parametrize(
    ("checks", "expected_checks"),
    [
        (
            {"postgresql": False, "redis": True},
            {"postgresql": "error", "redis": "ok"},
        ),
        (
            {"postgresql": True, "redis": False},
            {"postgresql": "ok", "redis": "error"},
        ),
    ],
)
def test_readiness_when_a_dependency_is_unavailable(
    client: TestClient,
    checks: dict[str, bool],
    expected_checks: dict[str, str],
) -> None:
    async def unavailable() -> dict[str, bool]:
        return checks

    app.dependency_overrides[get_readiness_status] = unavailable

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "checks": expected_checks}
