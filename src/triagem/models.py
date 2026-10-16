from __future__ import annotations

from datetime import datetime
from enum import Enum, StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from triagem.core.database import Base


def _enum_values(enum_class: type[Enum]) -> list[str]:
    return [str(member.value) for member in enum_class]


class TicketStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    CLASSIFIED = "classified"
    FAILED = "failed"


class ClassificationCategory(StrEnum):
    PAYMENT = "pagamento"
    DELIVERY = "entrega"
    PRODUCT = "produto"
    ACCOUNT = "conta"
    OTHER = "outros"


class TicketPriority(StrEnum):
    LOW = "baixa"
    MEDIUM = "media"
    HIGH = "alta"
    URGENT = "urgente"


class Sentiment(StrEnum):
    POSITIVE = "positivo"
    NEUTRAL = "neutro"
    NEGATIVE = "negativo"


class JobType(StrEnum):
    CLASSIFICATION = "classification"
    ALERT_DELIVERY = "alert_delivery"


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_SCHEDULED = "retry_scheduled"
    COMPLETED = "completed"
    FAILED = "failed"


class AlertDeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    TRANSIENT_FAILURE = "transient_failure"
    PERMANENT_FAILURE = "permanent_failure"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Ticket(TimestampMixin, Base):
    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("external_id", name="uq_tickets_external_id"),
        Index("ix_tickets_received_at", "received_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_email: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    source_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[TicketStatus] = mapped_column(
        SqlEnum(
            TicketStatus,
            name="ticket_status",
            values_callable=_enum_values,
            validate_strings=True,
        ),
        default=TicketStatus.RECEIVED,
        server_default=TicketStatus.RECEIVED.value,
        nullable=False,
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    classifications: Mapped[list[Classification]] = relationship(
        back_populates="ticket", passive_deletes=True
    )
    processing_jobs: Mapped[list[ProcessingJob]] = relationship(
        back_populates="ticket", passive_deletes=True
    )


class ClassifierVersion(Base):
    __tablename__ = "classifier_versions"
    __table_args__ = (
        UniqueConstraint(
            "provider", "model_name", "version", name="uq_classifier_versions_identity"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    output_schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    classifications: Mapped[list[Classification]] = relationship(
        back_populates="classifier_version", passive_deletes=True
    )


class Classification(Base):
    __tablename__ = "classifications"
    __table_args__ = (
        CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="ck_input_tokens"),
        CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="ck_output_tokens"),
        CheckConstraint(
            "provider_latency_ms IS NULL OR provider_latency_ms >= 0",
            name="ck_provider_latency_ms",
        ),
        Index("ix_classifications_ticket_id", "ticket_id"),
        Index("ix_classifications_classifier_version_id", "classifier_version_id"),
        Index("ix_classifications_created_at", "created_at"),
        Index("ix_classifications_category_created_at", "category", "created_at"),
        Index("ix_classifications_priority_created_at", "priority", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    ticket_id: Mapped[UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="RESTRICT"), nullable=False
    )
    classifier_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("classifier_versions.id", ondelete="RESTRICT"), nullable=False
    )
    category: Mapped[ClassificationCategory] = mapped_column(
        SqlEnum(
            ClassificationCategory,
            name="classification_category",
            values_callable=_enum_values,
            validate_strings=True,
        ),
        nullable=False,
    )
    priority: Mapped[TicketPriority] = mapped_column(
        SqlEnum(
            TicketPriority,
            name="ticket_priority",
            values_callable=_enum_values,
            validate_strings=True,
        ),
        nullable=False,
    )
    sentiment: Mapped[Sentiment] = mapped_column(
        SqlEnum(
            Sentiment, name="sentiment", values_callable=_enum_values, validate_strings=True
        ),
        nullable=False,
    )
    suggested_response: Mapped[str] = mapped_column(Text, nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    provider_latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticket: Mapped[Ticket] = relationship(back_populates="classifications")
    classifier_version: Mapped[ClassifierVersion] = relationship(
        back_populates="classifications"
    )
    processing_jobs: Mapped[list[ProcessingJob]] = relationship(
        back_populates="classification", passive_deletes=True
    )
    alert_deliveries: Mapped[list[AlertDelivery]] = relationship(
        back_populates="classification", passive_deletes=True
    )


class ProcessingJob(TimestampMixin, Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        CheckConstraint("queue_priority BETWEEN 0 AND 100", name="ck_job_queue_priority"),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts",
            name="ck_job_attempts",
        ),
        Index("ix_processing_jobs_ticket_id", "ticket_id"),
        Index("ix_processing_jobs_classification_id", "classification_id"),
        Index(
            "ix_processing_jobs_polling",
            "status",
            "available_at",
            "queue_priority",
            postgresql_where=text("status IN ('pending', 'retry_scheduled')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    ticket_id: Mapped[UUID] = mapped_column(
        ForeignKey("tickets.id", ondelete="RESTRICT"), nullable=False
    )
    classification_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("classifications.id", ondelete="RESTRICT")
    )
    job_type: Mapped[JobType] = mapped_column(
        SqlEnum(JobType, name="job_type", values_callable=_enum_values, validate_strings=True),
        nullable=False,
    )
    queue_priority: Mapped[int] = mapped_column(
        SmallInteger, default=0, server_default=text("0"), nullable=False
    )
    status: Mapped[JobStatus] = mapped_column(
        SqlEnum(JobStatus, name="job_status", values_callable=_enum_values, validate_strings=True),
        default=JobStatus.PENDING,
        server_default=JobStatus.PENDING.value,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, default=3, server_default=text("3"), nullable=False
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(255))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_message: Mapped[str | None] = mapped_column(String(1000))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ticket: Mapped[Ticket] = relationship(back_populates="processing_jobs")
    classification: Mapped[Classification | None] = relationship(back_populates="processing_jobs")


class AlertDelivery(TimestampMixin, Base):
    __tablename__ = "alert_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "classification_id", "attempt_number", name="uq_alert_delivery_attempt"
        ),
        CheckConstraint("attempt_number > 0", name="ck_alert_attempt_number"),
        CheckConstraint(
            "http_status_code IS NULL OR http_status_code BETWEEN 100 AND 599",
            name="ck_alert_http_status",
        ),
        Index("ix_alert_deliveries_classification_id", "classification_id"),
        Index("ix_alert_deliveries_status_created_at", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    classification_id: Mapped[UUID] = mapped_column(
        ForeignKey("classifications.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AlertDeliveryStatus] = mapped_column(
        SqlEnum(
            AlertDeliveryStatus,
            name="alert_delivery_status",
            values_callable=_enum_values,
            validate_strings=True,
        ),
        default=AlertDeliveryStatus.PENDING,
        server_default=AlertDeliveryStatus.PENDING.value,
        nullable=False,
    )
    request_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    http_status_code: Mapped[int | None] = mapped_column(Integer)
    response_body_excerpt: Mapped[str | None] = mapped_column(String(1000))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    classification: Mapped[Classification] = relationship(back_populates="alert_deliveries")
