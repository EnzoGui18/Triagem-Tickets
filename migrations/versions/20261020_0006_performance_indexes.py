"""Align indexes with worker polling and latest-classification queries.

Revision ID: 20261020_0006
Revises: 20261019_0005
Create Date: 2026-10-20 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261020_0006"
down_revision: str | None = "20261019_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_processing_jobs_polling", table_name="processing_jobs")
    op.create_index(
        "ix_processing_jobs_polling",
        "processing_jobs",
        [
            "job_type",
            sa.text("queue_priority DESC"),
            "available_at",
            "created_at",
            "id",
        ],
        postgresql_where=sa.text("status IN ('pending', 'retry_scheduled')"),
    )
    op.create_index(
        "ix_processing_jobs_lease_recovery",
        "processing_jobs",
        ["job_type", "locked_at"],
        postgresql_where=sa.text("status = 'processing'"),
    )
    op.create_index(
        "ix_classifications_ticket_latest",
        "classifications",
        ["ticket_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_classifications_ticket_latest",
        table_name="classifications",
    )
    op.drop_index(
        "ix_processing_jobs_lease_recovery",
        table_name="processing_jobs",
        postgresql_where=sa.text("status = 'processing'"),
    )
    op.drop_index(
        "ix_processing_jobs_polling",
        table_name="processing_jobs",
        postgresql_where=sa.text("status IN ('pending', 'retry_scheduled')"),
    )
    op.create_index(
        "ix_processing_jobs_polling",
        "processing_jobs",
        ["status", "available_at", "queue_priority"],
        postgresql_where=sa.text("status IN ('pending', 'retry_scheduled')"),
    )
