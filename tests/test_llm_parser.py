import json
from typing import Any

import pytest

from triagem.llm.parser import (
    LLMOutputParseError,
    LLMOutputValidationError,
    parse_classification_output,
)
from triagem.models import ClassificationCategory, Sentiment, TicketPriority


def _valid_payload() -> dict[str, str]:
    return {
        "categoria": "entrega",
        "prioridade": "alta",
        "sentimento": "negativo",
        "sugestao_resposta": "Vamos verificar a entrega do seu pedido.",
    }


def test_parser_accepts_valid_json() -> None:
    parsed = parse_classification_output(json.dumps(_valid_payload()))

    assert parsed.categoria == ClassificationCategory.DELIVERY
    assert parsed.prioridade == TicketPriority.HIGH
    assert parsed.sentimento == Sentiment.NEGATIVE
    assert parsed.sugestao_resposta == "Vamos verificar a entrega do seu pedido."


def test_parser_accepts_json_in_markdown_fence() -> None:
    output = f"```json\n{json.dumps(_valid_payload())}\n```"

    assert parse_classification_output(output).categoria == ClassificationCategory.DELIVERY


def test_parser_accepts_small_text_before_and_after_json() -> None:
    output = f"Classificação solicitada:\n{json.dumps(_valid_payload())}\nFim."

    assert parse_classification_output(output).prioridade == TicketPriority.HIGH


def test_parser_rejects_truncated_json() -> None:
    output = json.dumps(_valid_payload())[:-12]

    with pytest.raises(LLMOutputParseError):
        parse_classification_output(output)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"remove": "categoria"}, id="missing-field"),
        pytest.param({"categoria": "financeiro"}, id="invalid-category"),
        pytest.param({"prioridade": "critical"}, id="invalid-priority"),
        pytest.param({"sentimento": "irritado"}, id="invalid-sentiment"),
    ],
)
def test_parser_rejects_semantically_invalid_output(mutation: dict[str, Any]) -> None:
    payload = _valid_payload()
    field_to_remove = mutation.get("remove")
    if isinstance(field_to_remove, str):
        payload.pop(field_to_remove)
    else:
        payload.update(mutation)

    with pytest.raises(LLMOutputValidationError):
        parse_classification_output(json.dumps(payload))


def test_parser_rejects_completely_invalid_output() -> None:
    with pytest.raises(LLMOutputParseError):
        parse_classification_output("não foi possível classificar")


def test_parser_rejects_ambiguous_multiple_json_objects() -> None:
    encoded = json.dumps(_valid_payload())

    with pytest.raises(LLMOutputParseError):
        parse_classification_output(f"{encoded}\n{encoded}")
