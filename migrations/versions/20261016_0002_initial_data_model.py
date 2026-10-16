"""Create the initial data model.

Revision ID: 20261016_0002
Revises: 20261016_0001
Create Date: 2026-10-16 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261016_0002"
down_revision: str | None = "20261016_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ticket_status = postgresql.ENUM(
    "received", "processing", "classified", "failed", name="ticket_status", create_type=False
)
classification_category = postgresql.ENUM(
    "pagamento",
    "entrega",
    "produto",
    "conta",
    "outros",
    name="classification_category",
    create_type=False,
)
ticket_priority = postgresql.ENUM(
    "baixa", "media", "alta", "urgente", name="ticket_priority", create_type=False
)
sentiment = postgresql.ENUM(
    "positivo", "neutro", "negativo", name="sentiment", create_type=False
)
job_type = postgresql.ENUM(
    "classification", "alert_delivery", name="job_type", create_type=False
)
job_status = postgresql.ENUM(
    "pending",
    "processing",
    "retry_scheduled",
    "completed",
    "failed",
    name="job_status",
    create_type=False,
)
alert_delivery_status = postgresql.ENUM(
    "pending",
    "delivered",
    "transient_failure",
    "permanent_failure",
    name="alert_delivery_status",
    create_type=False,
)

enum_types = (
    ticket_status,
    classification_category,
    ticket_priority,
    sentiment,
    job_type,
    job_status,
    alert_delivery_status,
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in enum_types:
        enum_type.create(bind, checkfirst=False)

    op.create_table(
        "tickets",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("customer_external_id", sa.String(length=255), nullable=False),
        sa.Column("customer_name", sa.String(length=255), nullable=False),
        sa.Column("customer_email", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("status", ticket_status, server_default="received", nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tickets"),
        sa.UniqueConstraint("external_id", name="uq_tickets_external_id"),
    )
    op.create_index("ix_tickets_received_at", "tickets", ["received_at"])

    op.create_table(
        "classifier_versions",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=100), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("output_schema_version", sa.String(length=50), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_classifier_versions"),
        sa.UniqueConstraint(
            "provider", "model_name", "version", name="uq_classifier_versions_identity"
        ),
    )

    op.create_table(
        "classifications",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("classifier_version_id", sa.Uuid(), nullable=False),
        sa.Column("category", classification_category, nullable=False),
        sa.Column("priority", ticket_priority, nullable=False),
        sa.Column("sentiment", sentiment, nullable=False),
        sa.Column("suggested_response", sa.Text(), nullable=False),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("provider_latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="ck_input_tokens"),
        sa.CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="ck_output_tokens"),
        sa.CheckConstraint(
            "provider_latency_ms IS NULL OR provider_latency_ms >= 0",
            name="ck_provider_latency_ms",
        ),
        sa.ForeignKeyConstraint(
            ["classifier_version_id"],
            ["classifier_versions.id"],
            name="fk_classifications_classifier_version_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name="fk_classifications_ticket_id", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_classifications"),
    )
    op.create_index("ix_classifications_ticket_id", "classifications", ["ticket_id"])
    op.create_index(
        "ix_classifications_classifier_version_id",
        "classifications",
        ["classifier_version_id"],
    )
    op.create_index("ix_classifications_created_at", "classifications", ["created_at"])
    op.create_index(
        "ix_classifications_category_created_at", "classifications", ["category", "created_at"]
    )
    op.create_index(
        "ix_classifications_priority_created_at", "classifications", ["priority", "created_at"]
    )

    op.create_table(
        "processing_jobs",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("classification_id", sa.Uuid(), nullable=True),
        sa.Column("job_type", job_type, nullable=False),
        sa.Column("queue_priority", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("status", job_status, server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=255), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_message", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("queue_priority BETWEEN 0 AND 100", name="ck_job_queue_priority"),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts",
            name="ck_job_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["classification_id"],
            ["classifications.id"],
            name="fk_processing_jobs_classification_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name="fk_processing_jobs_ticket_id", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_processing_jobs"),
    )
    op.create_index("ix_processing_jobs_ticket_id", "processing_jobs", ["ticket_id"])
    op.create_index(
        "ix_processing_jobs_classification_id", "processing_jobs", ["classification_id"]
    )
    op.create_index(
        "ix_processing_jobs_polling",
        "processing_jobs",
        ["status", "available_at", "queue_priority"],
        postgresql_where=sa.text("status IN ('pending', 'retry_scheduled')"),
    )

    op.create_table(
        "alert_deliveries",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("classification_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", alert_delivery_status, server_default="pending", nullable=False),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column("http_status_code", sa.Integer(), nullable=True),
        sa.Column("response_body_excerpt", sa.String(length=1000), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempt_number > 0", name="ck_alert_attempt_number"),
        sa.CheckConstraint(
            "http_status_code IS NULL OR http_status_code BETWEEN 100 AND 599",
            name="ck_alert_http_status",
        ),
        sa.ForeignKeyConstraint(
            ["classification_id"],
            ["classifications.id"],
            name="fk_alert_deliveries_classification_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_alert_deliveries"),
        sa.UniqueConstraint(
            "classification_id", "attempt_number", name="uq_alert_delivery_attempt"
        ),
    )
    op.create_index(
        "ix_alert_deliveries_classification_id", "alert_deliveries", ["classification_id"]
    )
    op.create_index(
        "ix_alert_deliveries_status_created_at",
        "alert_deliveries",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_alert_deliveries_status_created_at", table_name="alert_deliveries")
    op.drop_index("ix_alert_deliveries_classification_id", table_name="alert_deliveries")
    op.drop_table("alert_deliveries")

    op.drop_index("ix_processing_jobs_polling", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_classification_id", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_ticket_id", table_name="processing_jobs")
    op.drop_table("processing_jobs")

    op.drop_index("ix_classifications_priority_created_at", table_name="classifications")
    op.drop_index("ix_classifications_category_created_at", table_name="classifications")
    op.drop_index("ix_classifications_created_at", table_name="classifications")
    op.drop_index("ix_classifications_classifier_version_id", table_name="classifications")
    op.drop_index("ix_classifications_ticket_id", table_name="classifications")
    op.drop_table("classifications")

    op.drop_table("classifier_versions")
    op.drop_index("ix_tickets_received_at", table_name="tickets")
    op.drop_table("tickets")

    bind = op.get_bind()
    for enum_type in reversed(enum_types):
        enum_type.drop(bind, checkfirst=False)
