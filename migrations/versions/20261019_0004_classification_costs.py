"""Add token costs to classifications.

Revision ID: 20261019_0004
Revises: 20261017_0003
Create Date: 2026-10-19 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261019_0004"
down_revision: str | None = "20261017_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cost_type = sa.Numeric(precision=20, scale=8)
    op.add_column("classifications", sa.Column("input_cost", cost_type))
    op.add_column("classifications", sa.Column("output_cost", cost_type))
    op.add_column("classifications", sa.Column("total_cost", cost_type))

    op.execute(
        sa.text(
            "UPDATE classifications SET "
            "input_cost = input_tokens * CAST(0.40 AS NUMERIC) / CAST(1000000 AS NUMERIC), "
            "output_cost = output_tokens * CAST(1.60 AS NUMERIC) / CAST(1000000 AS NUMERIC), "
            "total_cost = (input_tokens * CAST(0.40 AS NUMERIC) + "
            "output_tokens * CAST(1.60 AS NUMERIC)) / CAST(1000000 AS NUMERIC) "
            "WHERE input_tokens IS NOT NULL AND output_tokens IS NOT NULL"
        )
    )

    op.create_check_constraint(
        "ck_classification_costs_complete",
        "classifications",
        "(input_cost IS NULL AND output_cost IS NULL AND total_cost IS NULL) OR "
        "(input_cost IS NOT NULL AND output_cost IS NOT NULL AND total_cost IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_classification_costs_nonnegative",
        "classifications",
        "input_cost IS NULL OR (input_cost >= 0 AND output_cost >= 0 AND total_cost >= 0)",
    )
    op.create_check_constraint(
        "ck_classification_total_cost",
        "classifications",
        "total_cost IS NULL OR total_cost = input_cost + output_cost",
    )


def downgrade() -> None:
    op.drop_constraint("ck_classification_total_cost", "classifications", type_="check")
    op.drop_constraint("ck_classification_costs_nonnegative", "classifications", type_="check")
    op.drop_constraint("ck_classification_costs_complete", "classifications", type_="check")
    op.drop_column("classifications", "total_cost")
    op.drop_column("classifications", "output_cost")
    op.drop_column("classifications", "input_cost")
