from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from triagem.models import (
    ClassificationCategory,
    JobStatus,
    Sentiment,
    TicketPriority,
    TicketStatus,
)

ExternalId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
CustomerName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
Subject = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Channel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class CustomerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: ExternalId = Field(alias="id")
    name: CustomerName = Field(alias="nome")
    email: EmailStr


class TicketPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: ExternalId
    customer: CustomerPayload = Field(alias="cliente")
    subject: Subject = Field(alias="assunto")
    message: str = Field(alias="mensagem", min_length=1)
    channel: Channel = Field(alias="canal")
    source_created_at: datetime = Field(alias="criado_em")

    @field_validator("message")
    @classmethod
    def message_must_have_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("mensagem não pode conter apenas espaços")
        return value

    @field_validator("source_created_at")
    @classmethod
    def source_created_at_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("criado_em deve conter fuso horário")
        return value


class TicketAcceptedResponse(BaseModel):
    id: UUID
    external_id: str
    status: TicketStatus
    received_at: datetime = Field(serialization_alias="recebido_em")


class TicketListFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: ClassificationCategory | None = Field(default=None, alias="categoria")
    priority: TicketPriority | None = Field(default=None, alias="prioridade")
    received_from: datetime | None = Field(default=None, alias="inicio")
    received_until: datetime | None = Field(default=None, alias="fim")
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_period(self) -> TicketListFilters:
        for field_name, value in (
            ("inicio", self.received_from),
            ("fim", self.received_until),
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{field_name} deve conter fuso horário")
        if (
            self.received_from is not None
            and self.received_until is not None
            and self.received_from > self.received_until
        ):
            raise ValueError("inicio deve ser anterior ou igual a fim")
        return self


class TicketCustomerResponse(BaseModel):
    external_id: str = Field(serialization_alias="id")
    name: str = Field(serialization_alias="nome")
    email: EmailStr


class TicketClassificationResponse(BaseModel):
    id: UUID
    category: ClassificationCategory = Field(serialization_alias="categoria")
    priority: TicketPriority = Field(serialization_alias="prioridade")
    sentiment: Sentiment = Field(serialization_alias="sentimento")
    suggested_response: str = Field(serialization_alias="sugestao_resposta")
    created_at: datetime = Field(serialization_alias="criado_em")


class TicketProcessingResponse(BaseModel):
    job_id: UUID
    status: JobStatus
    attempt_count: int = Field(serialization_alias="tentativas")
    max_attempts: int = Field(serialization_alias="max_tentativas")
    available_at: datetime = Field(serialization_alias="disponivel_em")


class TicketDetailResponse(BaseModel):
    id: UUID
    external_id: str
    customer: TicketCustomerResponse = Field(serialization_alias="cliente")
    subject: str = Field(serialization_alias="assunto")
    message: str = Field(serialization_alias="mensagem")
    channel: str = Field(serialization_alias="canal")
    source_created_at: datetime = Field(serialization_alias="criado_em")
    received_at: datetime = Field(serialization_alias="recebido_em")
    status: TicketStatus
    current_classification: TicketClassificationResponse | None = Field(
        serialization_alias="classificacao_atual"
    )
    processing: TicketProcessingResponse | None = Field(serialization_alias="processamento")


class TicketListItemResponse(BaseModel):
    id: UUID
    external_id: str
    subject: str = Field(serialization_alias="assunto")
    channel: str = Field(serialization_alias="canal")
    source_created_at: datetime = Field(serialization_alias="criado_em")
    received_at: datetime = Field(serialization_alias="recebido_em")
    status: TicketStatus
    current_classification: TicketClassificationResponse | None = Field(
        serialization_alias="classificacao_atual"
    )


class TicketListResponse(BaseModel):
    items: list[TicketListItemResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class TicketTimelineEventType(StrEnum):
    TICKET_RECEIVED = "ticket_received"
    CLASSIFICATION_QUEUED = "classification_queued"
    CLASSIFICATION_STARTED = "classification_started"
    CLASSIFICATION_RETRY = "classification_retry"
    CLASSIFICATION_COMPLETED = "classification_completed"
    CLASSIFICATION_FAILED = "classification_failed"
    ALERT_CREATED = "alert_created"
    ALERT_ATTEMPT = "alert_attempt"
    ALERT_RETRY = "alert_retry"
    ALERT_DELIVERED = "alert_delivered"
    ALERT_FAILED = "alert_failed"


class TicketTimelineEventResponse(BaseModel):
    event_type: TicketTimelineEventType = Field(serialization_alias="tipo")
    occurred_at: datetime = Field(serialization_alias="ocorrido_em")
    job_id: UUID | None = None
    classification_id: UUID | None = None
    alert_id: UUID | None = None
    attempt_number: int | None = Field(default=None, serialization_alias="tentativa")
    status: str | None = None
    category: ClassificationCategory | None = Field(default=None, serialization_alias="categoria")
    priority: TicketPriority | None = Field(default=None, serialization_alias="prioridade")
    error_code: str | None = Field(default=None, serialization_alias="codigo_erro")
    error_message: str | None = Field(default=None, serialization_alias="erro")
    http_status_code: int | None = Field(default=None, serialization_alias="http_status")
    next_retry_at: datetime | None = Field(default=None, serialization_alias="proxima_tentativa_em")
    provider_request_id: str | None = None


class TicketTimelineResponse(BaseModel):
    external_id: str
    events: list[TicketTimelineEventResponse] = Field(serialization_alias="eventos")
