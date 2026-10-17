"""Add provider metadata to classifications.

Revision ID: 20261017_0003
Revises: 20261016_0002
Create Date: 2026-10-17 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261017_0003"
down_revision: str | None = "20261016_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("classifications", sa.Column("provider_model", sa.String(length=255)))
    op.add_column("classifications", sa.Column("raw_output", sa.Text()))


def downgrade() -> None:
    op.drop_column("classifications", "raw_output")
    op.drop_column("classifications", "provider_model")
