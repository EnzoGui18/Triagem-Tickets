"""Create the initial database baseline.

Revision ID: 20261016_0001
Revises:
Create Date: 2026-10-16 00:00:00
"""

from collections.abc import Sequence

revision: str = "20261016_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the schema baseline for the bootstrap phase."""


def downgrade() -> None:
    """Remove the schema baseline for the bootstrap phase."""
