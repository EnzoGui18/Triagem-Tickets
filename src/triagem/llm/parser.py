from __future__ import annotations

import json
from typing import cast

from pydantic import ValidationError

from triagem.llm.schemas import ClassificationOutput

MAX_SURROUNDING_TEXT_LENGTH = 200


class LLMOutputError(Exception):
    """Base error for provider output that cannot be consumed safely."""


class LLMOutputParseError(LLMOutputError):
    """The provider output does not contain one safely extractable JSON object."""


class LLMOutputValidationError(LLMOutputError):
    """The extracted JSON object does not satisfy the classification contract."""


def _wrapper_is_safe(value: str) -> bool:
    stripped = value.strip()
    return len(stripped) <= MAX_SURROUNDING_TEXT_LENGTH and not any(
        character in stripped for character in "{}[]"
    )


def _extract_json_object(output_text: str) -> dict[str, object]:
    stripped = output_text.strip()
    if not stripped:
        raise LLMOutputParseError("LLM output is empty")

    try:
        decoded: object = json.loads(stripped)
    except json.JSONDecodeError:
        decoded = None
    else:
        if isinstance(decoded, dict):
            return cast(dict[str, object], decoded)
        raise LLMOutputParseError("LLM output must be a JSON object")

    decoder = json.JSONDecoder()
    candidates: list[dict[str, object]] = []
    for position, character in enumerate(stripped):
        if character != "{":
            continue
        try:
            candidate, consumed = decoder.raw_decode(stripped[position:])
        except json.JSONDecodeError:
            continue
        if not isinstance(candidate, dict):
            continue

        prefix = stripped[:position]
        suffix = stripped[position + consumed :]
        if _wrapper_is_safe(prefix) and _wrapper_is_safe(suffix):
            candidates.append(cast(dict[str, object], candidate))

    if len(candidates) != 1:
        raise LLMOutputParseError(
            "LLM output must contain exactly one safely extractable JSON object"
        )
    return candidates[0]


def parse_classification_output(output_text: str) -> ClassificationOutput:
    payload = _extract_json_object(output_text)
    try:
        return ClassificationOutput.model_validate(payload)
    except ValidationError as error:
        raise LLMOutputValidationError("LLM output violates the classification schema") from error
