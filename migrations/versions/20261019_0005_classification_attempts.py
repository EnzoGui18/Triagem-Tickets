"""Persist classification processing attempts.

Revision ID: 20261019_0005
Revises: 20261019_0004
Create Date: 2026-10-19 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261019_0005"
down_revision: str | None = "20261019_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

job_status = postgresql.ENUM(
    "pending",
    "processing",
    "retry_scheduled",
    "completed",
    "failed",
    name="job_status",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "classification_attempts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("processing_job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", job_status, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.CheckConstraint("attempt_number > 0", name="ck_classification_attempt_number"),
        sa.ForeignKeyConstraint(
            ["processing_job_id"],
            ["processing_jobs.id"],
            name="fk_classification_attempts_processing_job_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_classification_attempts"),
        sa.UniqueConstraint(
            "processing_job_id",
            "attempt_number",
            name="uq_classification_attempt_number",
        ),
    )
    op.create_index(
        "ix_classification_attempts_job_started_at",
        "classification_attempts",
        ["processing_job_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_classification_attempts_job_started_at",
        table_name="classification_attempts",
    )
    op.drop_table("classification_attempts")
