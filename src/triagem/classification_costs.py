from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

INPUT_PRICE_PER_MILLION_TOKENS = Decimal("0.40")
OUTPUT_PRICE_PER_MILLION_TOKENS = Decimal("1.60")
TOKENS_PER_MILLION = Decimal("1000000")


@dataclass(frozen=True, slots=True)
class ClassificationCosts:
    input_cost: Decimal
    output_cost: Decimal
    total_cost: Decimal


def calculate_classification_costs(
    *,
    input_tokens: int,
    output_tokens: int,
) -> ClassificationCosts:
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts cannot be negative")

    input_cost = (
        Decimal(input_tokens) * INPUT_PRICE_PER_MILLION_TOKENS / TOKENS_PER_MILLION
    )
    output_cost = (
        Decimal(output_tokens) * OUTPUT_PRICE_PER_MILLION_TOKENS / TOKENS_PER_MILLION
    )
    return ClassificationCosts(
        input_cost=input_cost,
        output_cost=output_cost,
        total_cost=input_cost + output_cost,
    )
