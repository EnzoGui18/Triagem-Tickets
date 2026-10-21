from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from triagem.llm.client import LLMClient
from triagem.llm.parser import parse_classification_output
from triagem.llm.prompt import (
    CLASSIFIER_OUTPUT_SCHEMA_VERSION,
    CLASSIFIER_PROMPT_VERSION,
    CLASSIFIER_V1_SYSTEM_PROMPT,
    build_classifier_input,
)
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
    input_tokens: int
    output_tokens: int
    provider_request_id: str | None = None
    provider_model: str | None = None
    raw_output: str | None = None
    provider_latency_ms: int | None = None
    input_truncated: bool = False


class Classifier(Protocol):
    identity: ClassifierIdentity

    async def classify(self, ticket: Ticket) -> ClassificationResult: ...


class LLMClassifier:
    def __init__(self, client: LLMClient) -> None:
        self._client = client
        self.identity = ClassifierIdentity(
            provider="llm-sim",
            model_name=client.model,
            version=CLASSIFIER_PROMPT_VERSION,
            system_prompt=CLASSIFIER_V1_SYSTEM_PROMPT,
            output_schema_version=CLASSIFIER_OUTPUT_SCHEMA_VERSION,
        )

    async def classify(self, ticket: Ticket) -> ClassificationResult:
        classifier_input = build_classifier_input(
            subject=ticket.subject,
            message=ticket.message,
        )
        generation = await self._client.generate(
            system=CLASSIFIER_V1_SYSTEM_PROMPT,
            input_text=classifier_input.text,
        )
        parsed = parse_classification_output(generation.output_text)
        return ClassificationResult(
            category=parsed.categoria,
            priority=parsed.prioridade,
            sentiment=parsed.sentimento,
            suggested_response=parsed.sugestao_resposta,
            provider_request_id=generation.id,
            provider_model=generation.model,
            raw_output=generation.output_text,
            input_tokens=generation.usage.input_tokens,
            output_tokens=generation.usage.output_tokens,
            provider_latency_ms=generation.latency_ms,
            input_truncated=classifier_input.truncated,
        )


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
