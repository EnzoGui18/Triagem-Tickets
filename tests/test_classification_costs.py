from decimal import Decimal

import pytest

from triagem.classification_costs import calculate_classification_costs


def test_calculates_input_output_and_total_costs_with_decimal() -> None:
    costs = calculate_classification_costs(input_tokens=31, output_tokens=12)

    assert costs.input_cost == Decimal("0.0000124")
    assert costs.output_cost == Decimal("0.0000192")
    assert costs.total_cost == Decimal("0.0000316")
    assert isinstance(costs.total_cost, Decimal)


def test_zero_tokens_have_zero_cost() -> None:
    costs = calculate_classification_costs(input_tokens=0, output_tokens=0)

    assert costs.input_cost == Decimal("0")
    assert costs.output_cost == Decimal("0")
    assert costs.total_cost == Decimal("0")


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    [(-1, 0), (0, -1)],
)
def test_negative_token_counts_are_rejected(input_tokens: int, output_tokens: int) -> None:
    with pytest.raises(ValueError, match="token counts cannot be negative"):
        calculate_classification_costs(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
