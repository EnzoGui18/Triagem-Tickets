from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from triagem.core.redis import redis_client
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


@dataclass(frozen=True, slots=True)
class FailurePolicy:
    retryable: bool
    error_code: str
    retry_after_seconds: float | None = None
    consume_attempt: bool = True


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
        if max_retry_delay < retry_delay:
            raise ValueError("max_retry_delay cannot be shorter than retry_delay")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")

        self._session_factory = session_factory
        self._classifier = classifier
        self.worker_id = worker_id
        self.lease_duration = lease_duration
        self.retry_delay = retry_delay
        self.max_retry_delay = max_retry_delay
        self._jitter_source = jitter_source
        self.poll_interval_seconds = poll_interval_seconds

    async def reserve_next_job(self) -> ReservedJob | None:
        async with self._session_factory() as session, session.begin():
            now = await session.scalar(select(func.clock_timestamp()))
            if now is None:  # pragma: no cover - PostgreSQL always returns a value
                raise RuntimeError("database clock is unavailable")

            await self._recover_expired_leases(session, now)

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
            )

    async def run_once(self) -> bool:
        reserved_job = await self.reserve_next_job()
        if reserved_job is None:
            return False

        try:
            classifier_version_id = await self._resolve_classifier_version_id()
            ticket = await self._load_ticket(reserved_job.ticket_id)
            result = await self._classifier.classify(ticket)
        except Exception as error:
            recorded = await self._record_failure(reserved_job, error)
            if recorded:
                logger.warning(
                    "classification_job_failed job_id=%s worker_id=%s error_type=%s",
                    reserved_job.id,
                    self.worker_id,
                    type(error).__name__,
                )
            return True

        classification_id = await self._complete_job(
            reserved_job,
            result,
            classifier_version_id,
        )
        if classification_id is None:
            logger.info(
                "classification_job_lease_lost job_id=%s worker_id=%s",
                reserved_job.id,
                self.worker_id,
            )
        else:
            logger.info(
                "classification_job_completed job_id=%s classification_id=%s worker_id=%s",
                reserved_job.id,
                classification_id,
                self.worker_id,
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

        exhausted_result = await session.execute(
            update(ProcessingJob)
            .where(lease_expired, ProcessingJob.attempt_count >= ProcessingJob.max_attempts)
            .values(
                status=JobStatus.FAILED,
                locked_at=None,
                locked_by=None,
                last_error_code="lease_exhausted",
                last_error_message="worker lease expired after the final attempt",
                completed_at=now,
                updated_at=now,
            )
            .returning(ProcessingJob.ticket_id)
        )
        exhausted_ticket_ids = list(exhausted_result.scalars())
        if exhausted_ticket_ids:
            await session.execute(
                update(Ticket)
                .where(Ticket.id.in_(exhausted_ticket_ids))
                .values(status=TicketStatus.FAILED, updated_at=now)
            )

        await session.execute(
            update(ProcessingJob)
            .where(lease_expired, ProcessingJob.attempt_count < ProcessingJob.max_attempts)
            .values(
                status=JobStatus.RETRY_SCHEDULED,
                available_at=now,
                locked_at=None,
                locked_by=None,
                last_error_code="lease_expired",
                last_error_message="worker lease expired before completion",
                updated_at=now,
            )
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
    ) -> UUID | None:
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
                created_at=now,
            )
            session.add(classification)
            await session.flush()

            if result.priority == TicketPriority.URGENT:
                ticket = await session.get(Ticket, job.ticket_id)
                if ticket is None:  # pragma: no cover - protected by the job foreign key
                    raise RuntimeError("job ticket was not found")
                alert_payload = build_alert_payload(
                    ticket,
                    category=result.category,
                    detected_at=now,
                )
                session.add_all(
                    [
                        AlertDelivery(
                            classification_id=classification.id,
                            attempt_number=1,
                            status=AlertDeliveryStatus.PENDING,
                            request_payload=alert_payload.model_dump(mode="json", by_alias=True),
                        ),
                        ProcessingJob(
                            ticket_id=job.ticket_id,
                            classification_id=classification.id,
                            job_type=JobType.ALERT_DELIVERY,
                            queue_priority=100,
                            available_at=now,
                        ),
                    ]
                )

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
            return classification.id

    async def _record_failure(self, reserved_job: ReservedJob, error: Exception) -> bool:
        async with self._session_factory() as session, session.begin():
            job = await self._owned_job(session, reserved_job)
            if job is None:
                return False

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
            return True

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
        async with self._session_factory() as session, session.begin():
            return await self._classifier_version_id(session)

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
    logging.basicConfig(level=get_settings().log_level)
    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
