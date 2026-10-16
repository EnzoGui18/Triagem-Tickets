from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
)

from triagem.models import TicketStatus

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
