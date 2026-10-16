from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from triagem.models import (
    ClassificationCategory,
    Sentiment,
    Ticket,
    TicketPriority,
)


@dataclass(frozen=True, slots=True)
class ClassifierIdentity:
    provider: str
    model_name: str
    version: str
    system_prompt: str
    output_schema_version: str


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    category: ClassificationCategory
    priority: TicketPriority
    sentiment: Sentiment
    suggested_response: str
    provider_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    provider_latency_ms: int | None = None


class Classifier(Protocol):
    identity: ClassifierIdentity

    async def classify(self, ticket: Ticket) -> ClassificationResult: ...


class FakeClassifier:
    identity = ClassifierIdentity(
        provider="fake",
        model_name="fake-classifier",
        version="1",
        system_prompt=(
            "Classificador determinístico usado antes da integração com o provedor de IA."
        ),
        output_schema_version="1",
    )

    async def classify(self, ticket: Ticket) -> ClassificationResult:
        del ticket
        return ClassificationResult(
            category=ClassificationCategory.OTHER,
            priority=TicketPriority.MEDIUM,
            sentiment=Sentiment.NEUTRAL,
            suggested_response=(
                "Recebemos seu contato e ele será analisado pela equipe responsável."
            ),
            input_tokens=0,
            output_tokens=0,
            provider_latency_ms=0,
        )
