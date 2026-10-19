from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from triagem.models import ClassificationCategory, Ticket


class AlertPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    external_id: str
    category: ClassificationCategory = Field(alias="categoria")
    priority: Literal["urgente"] = Field(alias="prioridade")
    summary: str = Field(alias="resumo", max_length=280)
    detected_at: datetime = Field(alias="detectado_em")


@dataclass(frozen=True, slots=True)
class AlertProviderResult:
    provider_alert_id: str | None
    response_body_excerpt: str | None


class AlertProviderError(Exception):
    def __init__(self, *, error_code: str, status_code: int | None = None) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.status_code = status_code


class AlertTransientError(AlertProviderError):
    pass


class AlertPermanentError(AlertProviderError):
    pass


class AlertSender(Protocol):
    async def deliver(self, payload: AlertPayload) -> AlertProviderResult: ...


def build_alert_summary(ticket: Ticket) -> str:
    normalized = " ".join(f"{ticket.subject.strip()}: {ticket.message.strip()}".split())
    return normalized[:280]


def build_alert_payload(
    ticket: Ticket,
    *,
    category: ClassificationCategory,
    detected_at: datetime,
) -> AlertPayload:
    return AlertPayload(
        external_id=ticket.external_id,
        category=category,
        priority="urgente",
        summary=build_alert_summary(ticket),
        detected_at=detected_at,
    )


class PlantaoClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 10.0,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._http_client = http_client

    async def deliver(self, payload: AlertPayload) -> AlertProviderResult:
        try:
            response = await self._http_client.post(
                f"{self._base_url}/alertas",
                json=payload.model_dump(mode="json", by_alias=True),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as error:
            raise AlertTransientError(error_code="plantao_timeout") from error
        except httpx.RequestError as error:
            raise AlertTransientError(error_code="plantao_network_failure") from error

        if response.status_code == httpx.codes.ACCEPTED:
            return AlertProviderResult(
                provider_alert_id=_provider_alert_id(response),
                response_body_excerpt=_response_excerpt(response),
            )
        if response.status_code == httpx.codes.SERVICE_UNAVAILABLE:
            raise AlertTransientError(
                error_code="plantao_http_503",
                status_code=response.status_code,
            )
        if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY:
            raise AlertPermanentError(
                error_code="plantao_invalid_payload",
                status_code=response.status_code,
            )
        if response.status_code >= 500:
            raise AlertTransientError(
                error_code=f"plantao_http_{response.status_code}",
                status_code=response.status_code,
            )
        raise AlertPermanentError(
            error_code=f"plantao_http_{response.status_code}",
            status_code=response.status_code,
        )


def _provider_alert_id(response: httpx.Response) -> str | None:
    try:
        body: Any = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        for key in ("id", "alert_id", "alerta_id"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value[:255]
    for header in ("X-Alert-ID", "X-Request-ID"):
        value = response.headers.get(header)
        if value:
            return str(value)[:255]
    return None


def _response_excerpt(response: httpx.Response) -> str | None:
    if not response.content:
        return None
    return response.text[:1000]
