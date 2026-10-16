from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from triagem.models import ClassificationCategory, Sentiment, TicketPriority


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    system: str = Field(min_length=1)
    input: str = Field(min_length=1)


class GenerationUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class ProviderGenerationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    output_text: str
    usage: GenerationUsage


class GenerationResult(ProviderGenerationResponse):
    status_code: int = Field(ge=100, le=599)


class ClassificationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categoria: ClassificationCategory
    prioridade: TicketPriority
    sentimento: Sentiment
    sugestao_resposta: str = Field(min_length=1)
