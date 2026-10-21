from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from uuid import UUID, uuid4

import httpx
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from triagem.alerting import build_alert_payload
from triagem.classification_costs import calculate_classification_costs
from triagem.classifiers import ClassificationResult, Classifier, LLMClassifier
from triagem.core.config import get_settings
from triagem.core.database import async_session_factory, engine
from triagem.core.metrics import (
    CLASSIFICATION_COMPLETED,
    CLASSIFICATION_DURATION,
    CLASSIFICATION_FAILED,
    CLASSIFICATION_QUEUE_DEPTH,
    CLASSIFICATION_QUEUE_WAIT,
    CLASSIFICATION_RETRY,
    LLM_COST_USD,
    LLM_ERRORS,
    TOTAL_PROCESSING_DURATION,
    start_worker_metrics_server,
)
from triagem.core.redis import redis_client
from triagem.core.structured_logging import configure_structured_logging, log_event
from triagem.llm.circuit_breaker import (
    CircuitBreakerError,
    CircuitOpenError,
    CircuitProbeFailureError,
    RedisCircuitBreaker,
)
from triagem.llm.client import (
    LLMAuthenticationError,
    LLMClient,
    LLMHTTPError,
    LLMInvalidRequestError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTransientProviderError,
    LLMTransportError,
)
from triagem.llm.parser import LLMOutputError
from triagem.llm.rate_limiter import RateLimiterError, RedisSlidingWindowRateLimiter
from triagem.models import (
    AlertDelivery,
    AlertDeliveryStatus,
    Classification,
    ClassificationAttempt,
    ClassifierVersion,
    JobStatus,
    JobType,
    ProcessingJob,
    Ticket,
    TicketPriority,
    TicketStatus,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReservedJob:
    id: UUID
    ticket_id: UUID
    locked_at: datetime
    attempt_count: int
    classification_attempt_id: UUID
    attempt_number: int


@dataclass(frozen=True, slots=True)
class FailurePolicy:
    retryable: bool
    error_code: str
    retry_after_seconds: float | None = None
    consume_attempt: bool = True


@dataclass(frozen=True, slots=True)
class RecordedClassificationFailure:
    status: JobStatus
    error_code: str
    next_retry_at: datetime | None


@dataclass(frozen=True, slots=True)
class CompletedClassification:
    classification_id: UUID
    alert_id: UUID | None
    total_cost: Decimal
    total_processing_seconds: float


class ClassifierVersionConflictError(Exception):
    """A version label was reused with a different immutable definition."""


def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at - now).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _failure_policy(error: Exception, now: datetime) -> FailurePolicy:
    if isinstance(error, ClassifierVersionConflictError):
        return FailurePolicy(retryable=False, error_code="classifier_version_conflict")
    if isinstance(error, CircuitOpenError):
        return FailurePolicy(
            retryable=True,
            error_code="llm_circuit_open",
            retry_after_seconds=error.retry_after_seconds,
            consume_attempt=False,
        )
    if isinstance(error, CircuitProbeFailureError):
        return FailurePolicy(
            retryable=True,
            error_code="llm_circuit_probe_failed",
            retry_after_seconds=error.retry_after_seconds,
            consume_attempt=False,
        )
    if isinstance(error, CircuitBreakerError):
        return FailurePolicy(
            retryable=True,
            error_code="circuit_breaker_unavailable",
            consume_attempt=False,
        )
    if isinstance(error, LLMRateLimitError):
        return FailurePolicy(
            retryable=True,
            error_code="llm_rate_limited",
            retry_after_seconds=_retry_after_seconds(error.retry_after, now),
        )
    if isinstance(error, LLMAuthenticationError):
        return FailurePolicy(retryable=False, error_code="llm_authentication_failed")
    if isinstance(error, LLMInvalidRequestError):
        error_code = (
            "llm_context_length_exceeded"
            if error.error_type == "context_length_exceeded"
            else "llm_invalid_request"
        )
        return FailurePolicy(retryable=False, error_code=error_code)
    if isinstance(error, LLMTransientProviderError):
        return FailurePolicy(retryable=True, error_code=f"llm_http_{error.status_code}")
    if isinstance(error, LLMTimeoutError):
        return FailurePolicy(retryable=True, error_code="llm_timeout")
    if isinstance(error, LLMTransportError):
        return FailurePolicy(retryable=True, error_code="llm_network_failure")
    if isinstance(error, (LLMInvalidResponseError, LLMOutputError)):
        return FailurePolicy(retryable=True, error_code="llm_invalid_output")
    if isinstance(error, LLMHTTPError):
        return FailurePolicy(retryable=False, error_code=f"llm_http_{error.status_code}")
    if isinstance(error, RateLimiterError):
        return FailurePolicy(
            retryable=True,
            error_code="rate_limiter_unavailable",
            consume_attempt=False,
        )
    return FailurePolicy(retryable=True, error_code="classification_unexpected_error")


class ClassificationWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        classifier: Classifier,
        *,
        worker_id: str,
        lease_duration: timedelta,
        retry_delay: timedelta,
        alert_max_attempts: int | None = None,
        max_retry_delay: timedelta = timedelta(minutes=5),
        jitter_source: Callable[[float, float], float] = random.uniform,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if not worker_id or len(worker_id) > 255:
            raise ValueError("worker_id must have between 1 and 255 characters")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if retry_delay < timedelta(0):
            raise ValueError("retry_delay cannot be negative")
        resolved_alert_max_attempts = (
            get_settings().alert_max_attempts
            if alert_max_attempts is None
            else alert_max_attempts
        )
        if resolved_alert_max_attempts <= 0:
            raise ValueError("alert_max_attempts must be positive")
        if max_retry_delay < retry_delay:
            raise ValueError("max_retry_delay cannot be shorter than retry_delay")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")

        self._session_factory = session_factory
        self._classifier = classifier
        self.worker_id = worker_id
        self.lease_duration = lease_duration
        self.retry_delay = retry_delay
        self.alert_max_attempts = resolved_alert_max_attempts
        self.max_retry_delay = max_retry_delay
        self._jitter_source = jitter_source
        self.poll_interval_seconds = poll_interval_seconds
        self._resolved_classifier_version_id: UUID | None = None

    async def reserve_next_job(self) -> ReservedJob | None:
        async with self._session_factory() as session, session.begin():
            now = await session.scalar(select(func.clock_timestamp()))
            if now is None:  # pragma: no cover - PostgreSQL always returns a value
                raise RuntimeError("database clock is unavailable")

            await self._recover_expired_leases(session, now)

            queue_depth = await session.scalar(
                select(func.count(ProcessingJob.id)).where(
                    ProcessingJob.job_type == JobType.CLASSIFICATION,
                    ProcessingJob.status.in_([JobStatus.PENDING, JobStatus.RETRY_SCHEDULED]),
                )
            )
            CLASSIFICATION_QUEUE_DEPTH.set(queue_depth or 0)

            job = await session.scalar(
                select(ProcessingJob)
                .where(
                    ProcessingJob.job_type == JobType.CLASSIFICATION,
                    ProcessingJob.status.in_([JobStatus.PENDING, JobStatus.RETRY_SCHEDULED]),
                    ProcessingJob.available_at <= now,
                    ProcessingJob.attempt_count < ProcessingJob.max_attempts,
                )
                .order_by(
                    ProcessingJob.queue_priority.desc(),
                    ProcessingJob.available_at,
                    ProcessingJob.created_at,
                    ProcessingJob.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None

            job.status = JobStatus.PROCESSING
            job.attempt_count += 1
            job.locked_at = now
            job.locked_by = self.worker_id
            job.last_error_code = None
            job.last_error_message = None
            job.updated_at = now
            attempt_number = int(
                await session.scalar(
                    select(func.coalesce(func.max(ClassificationAttempt.attempt_number), 0) + 1)
                    .where(ClassificationAttempt.processing_job_id == job.id)
                )
                or 1
            )
            if attempt_number == 1:
                CLASSIFICATION_QUEUE_WAIT.observe(
                    max(0.0, (now - job.created_at).total_seconds())
                )
            attempt = ClassificationAttempt(
                processing_job_id=job.id,
                attempt_number=attempt_number,
                status=JobStatus.PROCESSING,
                started_at=now,
            )
            session.add(attempt)
            await session.flush()
            await session.execute(
                update(Ticket)
                .where(Ticket.id == job.ticket_id)
                .values(status=TicketStatus.PROCESSING, updated_at=now)
            )

            return ReservedJob(
                id=job.id,
                ticket_id=job.ticket_id,
                locked_at=now,
                attempt_count=job.attempt_count,
                classification_attempt_id=attempt.id,
                attempt_number=attempt_number,
            )

    async def run_once(self) -> bool:
        reserved_job = await self.reserve_next_job()
        if reserved_job is None:
            return False

        processing_started_at = time.perf_counter()

        log_event(
            logger,
            logging.INFO,
            "classification_started",
            job_id=reserved_job.id,
            ticket_id=reserved_job.ticket_id,
            attempt_number=reserved_job.attempt_number,
            worker_id=self.worker_id,
        )

        try:
            classifier_version_id = await self._resolve_classifier_version_id()
            ticket = await self._load_ticket(reserved_job.ticket_id)
            result = await self._classifier.classify(ticket)
        except Exception as error:
            recorded = await self._record_failure(reserved_job, error)
            if recorded:
                if isinstance(error, LLMOutputError):
                    LLM_ERRORS.inc()
                if recorded.status == JobStatus.RETRY_SCHEDULED:
                    CLASSIFICATION_RETRY.inc()
                    event = "classification_retry"
                else:
                    CLASSIFICATION_FAILED.inc()
                    event = "classification_failed"
                log_event(
                    logger,
                    logging.WARNING,
                    event,
                    job_id=reserved_job.id,
                    ticket_id=reserved_job.ticket_id,
                    attempt_number=reserved_job.attempt_number,
                    worker_id=self.worker_id,
                    error_code=recorded.error_code,
                    next_retry_at=recorded.next_retry_at,
                )
            CLASSIFICATION_DURATION.observe(
                max(0.0, time.perf_counter() - processing_started_at)
            )
            return True

        completed = await self._complete_job(
            reserved_job,
            result,
            classifier_version_id,
        )
        if completed is None:
            log_event(
                logger,
                logging.INFO,
                "classification_lease_lost",
                job_id=reserved_job.id,
                ticket_id=reserved_job.ticket_id,
                attempt_number=reserved_job.attempt_number,
                worker_id=self.worker_id,
            )
        else:
            CLASSIFICATION_COMPLETED.inc()
            # Prometheus samples are floating point by protocol; the persisted Decimal remains
            # the source of truth for financial reporting.
            LLM_COST_USD.inc(float(completed.total_cost))
            TOTAL_PROCESSING_DURATION.observe(completed.total_processing_seconds)
            log_event(
                logger,
                logging.INFO,
                "classification_completed",
                job_id=reserved_job.id,
                ticket_id=reserved_job.ticket_id,
                classification_id=completed.classification_id,
                provider_request_id=result.provider_request_id,
                worker_id=self.worker_id,
            )
            if completed.alert_id is not None:
                log_event(
                    logger,
                    logging.INFO,
                    "alert_created",
                    job_id=reserved_job.id,
                    ticket_id=reserved_job.ticket_id,
                    classification_id=completed.classification_id,
                    alert_id=completed.alert_id,
                    worker_id=self.worker_id,
                )
        CLASSIFICATION_DURATION.observe(
            max(0.0, time.perf_counter() - processing_started_at)
        )
        return True

    async def run_until_idle(self) -> int:
        processed = 0
        while await self.run_once():
            processed += 1
        return processed

    async def run_forever(self) -> None:
        while True:
            if not await self.run_once():
                await asyncio.sleep(self.poll_interval_seconds)

    async def _recover_expired_leases(self, session: AsyncSession, now: datetime) -> None:
        lease_expired = and_(
            ProcessingJob.job_type == JobType.CLASSIFICATION,
            ProcessingJob.status == JobStatus.PROCESSING,
            or_(
                ProcessingJob.locked_at.is_(None),
                ProcessingJob.locked_at <= now - self.lease_duration,
            ),
        )

        expired_jobs = list(
            (
                await session.scalars(
                    select(ProcessingJob).where(lease_expired).with_for_update(skip_locked=True)
                )
            ).all()
        )
        for job in expired_jobs:
            exhausted = job.attempt_count >= job.max_attempts
            error_code = "lease_exhausted" if exhausted else "lease_expired"
            error_message = (
                "worker lease expired after the final attempt"
                if exhausted
                else "worker lease expired before completion"
            )
            attempt = await session.scalar(
                select(ClassificationAttempt)
                .where(
                    ClassificationAttempt.processing_job_id == job.id,
                    ClassificationAttempt.status == JobStatus.PROCESSING,
                )
                .order_by(ClassificationAttempt.attempt_number.desc())
                .limit(1)
                .with_for_update()
            )
            if attempt is not None:
                attempt.status = JobStatus.FAILED if exhausted else JobStatus.RETRY_SCHEDULED
                attempt.finished_at = now
                attempt.next_retry_at = None if exhausted else now
                attempt.error_code = error_code
                attempt.error_message = error_message

            job.status = JobStatus.FAILED if exhausted else JobStatus.RETRY_SCHEDULED
            job.available_at = now
            job.locked_at = None
            job.locked_by = None
            job.last_error_code = error_code
            job.last_error_message = error_message
            job.completed_at = now if exhausted else None
            job.updated_at = now
            if exhausted:
                await session.execute(
                    update(Ticket)
                    .where(Ticket.id == job.ticket_id)
                    .values(status=TicketStatus.FAILED, updated_at=now)
                )

    async def _load_ticket(self, ticket_id: UUID) -> Ticket:
        async with self._session_factory() as session:
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                raise RuntimeError("job ticket was not found")
            return ticket

    async def _complete_job(
        self,
        reserved_job: ReservedJob,
        result: ClassificationResult,
        classifier_version_id: UUID,
    ) -> CompletedClassification | None:
        async with self._session_factory() as session, session.begin():
            job = await self._owned_job(session, reserved_job)
            if job is None:
                return None

            now = await session.scalar(select(func.clock_timestamp()))
            if now is None:  # pragma: no cover - PostgreSQL always returns a value
                raise RuntimeError("database clock is unavailable")
            costs = calculate_classification_costs(
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
            classification = Classification(
                ticket_id=job.ticket_id,
                classifier_version_id=classifier_version_id,
                category=result.category,
                priority=result.priority,
                sentiment=result.sentiment,
                suggested_response=result.suggested_response,
                provider_request_id=result.provider_request_id,
                provider_model=result.provider_model,
                raw_output=result.raw_output,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                input_cost=costs.input_cost,
                output_cost=costs.output_cost,
                total_cost=costs.total_cost,
                provider_latency_ms=result.provider_latency_ms,
                input_truncated=result.input_truncated,
                created_at=now,
            )
            session.add(classification)
            await session.flush()

            attempt = await session.get(
                ClassificationAttempt,
                reserved_job.classification_attempt_id,
                with_for_update=True,
            )
            if attempt is None:  # pragma: no cover - created atomically with the reservation
                raise RuntimeError("classification attempt was not found")
            attempt.status = JobStatus.COMPLETED
            attempt.finished_at = now
            attempt.next_retry_at = None
            attempt.error_code = None
            attempt.error_message = None
            attempt.provider_request_id = result.provider_request_id

            alert_delivery: AlertDelivery | None = None
            if result.priority == TicketPriority.URGENT:
                ticket = await session.get(Ticket, job.ticket_id)
                if ticket is None:  # pragma: no cover - protected by the job foreign key
                    raise RuntimeError("job ticket was not found")
                alert_payload = build_alert_payload(
                    ticket,
                    category=result.category,
                    detected_at=now,
                )
                alert_delivery = AlertDelivery(
                    classification_id=classification.id,
                    attempt_number=1,
                    status=AlertDeliveryStatus.PENDING,
                    request_payload=alert_payload.model_dump(mode="json", by_alias=True),
                )
                session.add_all(
                    [
                        alert_delivery,
                        ProcessingJob(
                            ticket_id=job.ticket_id,
                            classification_id=classification.id,
                            job_type=JobType.ALERT_DELIVERY,
                            queue_priority=100,
                            max_attempts=self.alert_max_attempts,
                            available_at=now,
                        ),
                    ]
                )
                await session.flush()

            job.classification_id = classification.id
            job.status = JobStatus.COMPLETED
            job.locked_at = None
            job.locked_by = None
            job.completed_at = now
            job.updated_at = now
            await session.execute(
                update(Ticket)
                .where(Ticket.id == job.ticket_id)
                .values(status=TicketStatus.CLASSIFIED, updated_at=now)
            )
            return CompletedClassification(
                classification_id=classification.id,
                alert_id=alert_delivery.id if alert_delivery is not None else None,
                total_cost=costs.total_cost,
                total_processing_seconds=max(
                    0.0,
                    (now - job.created_at).total_seconds(),
                ),
            )

    async def _record_failure(
        self,
        reserved_job: ReservedJob,
        error: Exception,
    ) -> RecordedClassificationFailure | None:
        async with self._session_factory() as session, session.begin():
            job = await self._owned_job(session, reserved_job)
            if job is None:
                return None

            now = await session.scalar(select(func.clock_timestamp()))
            if now is None:  # pragma: no cover - PostgreSQL always returns a value
                raise RuntimeError("database clock is unavailable")
            policy = _failure_policy(error, now)
            if not policy.consume_attempt:
                job.attempt_count = max(0, job.attempt_count - 1)
            job.locked_at = None
            job.locked_by = None
            job.last_error_code = policy.error_code
            job.last_error_message = (
                "retryable classification failure"
                if policy.retryable
                else "permanent classification failure"
            )
            job.updated_at = now

            if policy.retryable and job.attempt_count < job.max_attempts:
                job.status = JobStatus.RETRY_SCHEDULED
                job.available_at = now + self._retry_delay_for(
                    attempt_count=job.attempt_count,
                    retry_after_seconds=policy.retry_after_seconds,
                )
            else:
                job.status = JobStatus.FAILED
                job.completed_at = now
                await session.execute(
                    update(Ticket)
                    .where(Ticket.id == job.ticket_id)
                    .values(status=TicketStatus.FAILED, updated_at=now)
                )

            attempt = await session.get(
                ClassificationAttempt,
                reserved_job.classification_attempt_id,
                with_for_update=True,
            )
            if attempt is None:  # pragma: no cover - created atomically with the reservation
                raise RuntimeError("classification attempt was not found")
            attempt.status = job.status
            attempt.finished_at = now
            attempt.next_retry_at = (
                job.available_at if job.status == JobStatus.RETRY_SCHEDULED else None
            )
            attempt.error_code = policy.error_code
            attempt.error_message = job.last_error_message
            return RecordedClassificationFailure(
                status=job.status,
                error_code=policy.error_code,
                next_retry_at=(
                    job.available_at if job.status == JobStatus.RETRY_SCHEDULED else None
                ),
            )

    def _retry_delay_for(
        self,
        *,
        attempt_count: int,
        retry_after_seconds: float | None,
    ) -> timedelta:
        exponent = min(max(0, attempt_count - 1), 30)
        maximum_seconds = self.max_retry_delay.total_seconds()
        exponential_seconds = min(
            self.retry_delay.total_seconds() * (2**exponent),
            maximum_seconds,
        )
        jitter_ceiling = min(
            exponential_seconds * 0.25,
            maximum_seconds - exponential_seconds,
        )
        jitter_seconds = self._jitter_source(0.0, jitter_ceiling)
        scheduled_seconds = exponential_seconds + min(max(0.0, jitter_seconds), jitter_ceiling)
        if retry_after_seconds is not None:
            scheduled_seconds = max(scheduled_seconds, retry_after_seconds)
        return timedelta(seconds=scheduled_seconds)

    async def _owned_job(
        self,
        session: AsyncSession,
        reserved_job: ReservedJob,
    ) -> ProcessingJob | None:
        return await session.scalar(
            select(ProcessingJob)
            .where(
                ProcessingJob.id == reserved_job.id,
                ProcessingJob.status == JobStatus.PROCESSING,
                ProcessingJob.locked_by == self.worker_id,
                ProcessingJob.attempt_count == reserved_job.attempt_count,
            )
            .with_for_update()
        )

    async def _resolve_classifier_version_id(self) -> UUID:
        if self._resolved_classifier_version_id is not None:
            return self._resolved_classifier_version_id
        async with self._session_factory() as session, session.begin():
            classifier_version_id = await self._classifier_version_id(session)
        self._resolved_classifier_version_id = classifier_version_id
        return classifier_version_id

    async def _classifier_version_id(self, session: AsyncSession) -> UUID:
        identity = self._classifier.identity
        inserted_id = await session.scalar(
            insert(ClassifierVersion)
            .values(
                provider=identity.provider,
                model_name=identity.model_name,
                version=identity.version,
                system_prompt=identity.system_prompt,
                output_schema_version=identity.output_schema_version,
            )
            .on_conflict_do_nothing(constraint="uq_classifier_versions_identity")
            .returning(ClassifierVersion.id)
        )
        if inserted_id is not None:
            return inserted_id

        existing = await session.scalar(
            select(ClassifierVersion).where(
                ClassifierVersion.provider == identity.provider,
                ClassifierVersion.model_name == identity.model_name,
                ClassifierVersion.version == identity.version,
            )
        )
        if existing is None:  # pragma: no cover - protected by the unique constraint
            raise RuntimeError("classifier version could not be resolved")
        if (
            existing.system_prompt != identity.system_prompt
            or existing.output_schema_version != identity.output_schema_version
        ):
            raise ClassifierVersionConflictError(
                "classifier version identity conflicts with its stored definition"
            )
        return existing.id


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"


async def _run_worker() -> None:
    settings = get_settings()
    configure_structured_logging(settings.log_level)
    start_worker_metrics_server(settings.metrics_port)
    try:
        async with httpx.AsyncClient() as http_client:
            rate_limiter = RedisSlidingWindowRateLimiter(
                redis_client,
                rate_per_second=settings.llm_rate_limit_rps,
            )
            circuit_breaker = RedisCircuitBreaker(
                redis_client,
                failure_threshold=settings.llm_circuit_breaker_failure_threshold,
                recovery_timeout_seconds=(
                    settings.llm_circuit_breaker_recovery_timeout_seconds
                ),
            )
            llm_client = LLMClient(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key.get_secret_value(),
                model=settings.llm_model,
                timeout_seconds=settings.llm_timeout_seconds,
                rate_limiter=rate_limiter,
                circuit_breaker=circuit_breaker,
                max_concurrency=settings.llm_max_concurrency,
                http_client=http_client,
            )
            worker = ClassificationWorker(
                async_session_factory,
                LLMClassifier(llm_client),
                worker_id=_worker_id(),
                lease_duration=timedelta(seconds=settings.classification_worker_lease_seconds),
                retry_delay=timedelta(seconds=settings.classification_worker_retry_delay_seconds),
                alert_max_attempts=settings.alert_max_attempts,
                max_retry_delay=timedelta(
                    seconds=settings.classification_worker_max_retry_delay_seconds
                ),
                poll_interval_seconds=settings.classification_worker_poll_interval_seconds,
            )
            await worker.run_forever()
    finally:
        await redis_client.aclose()
        await engine.dispose()


def main() -> None:
    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
