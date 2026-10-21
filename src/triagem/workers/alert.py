from __future__ import annotations

import asyncio
import logging
import os
import random
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from triagem.alerting import (
    AlertPayload,
    AlertPermanentError,
    AlertProviderError,
    AlertProviderResult,
    AlertSender,
    AlertTransientError,
    PlantaoClient,
)
from triagem.core.config import get_settings
from triagem.core.database import async_session_factory, engine
from triagem.core.metrics import (
    ALERTS_DELIVERED,
    ALERTS_RETRY,
    URGENT_ALERT_END_TO_END,
    URGENT_ALERT_SLA_THRESHOLD_SECONDS,
    URGENT_ALERT_SLA_VIOLATIONS,
    start_worker_metrics_server,
)
from triagem.core.structured_logging import configure_structured_logging, log_event
from triagem.models import (
    AlertDelivery,
    AlertDeliveryStatus,
    JobStatus,
    JobType,
    ProcessingJob,
    Ticket,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReservedAlertJob:
    id: UUID
    ticket_id: UUID
    classification_id: UUID
    alert_delivery_id: UUID
    attempt_count: int
    request_payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class AlertFailurePolicy:
    retryable: bool
    error_code: str
    http_status_code: int | None = None


@dataclass(frozen=True, slots=True)
class RecordedAlertFailure:
    status: JobStatus
    error_code: str
    next_retry_at: datetime | None


@dataclass(frozen=True, slots=True)
class CompletedAlert:
    end_to_end_seconds: float


def _failure_policy(error: Exception) -> AlertFailurePolicy:
    if isinstance(error, AlertTransientError):
        return AlertFailurePolicy(
            retryable=True,
            error_code=error.error_code,
            http_status_code=error.status_code,
        )
    if isinstance(error, AlertPermanentError):
        return AlertFailurePolicy(
            retryable=False,
            error_code=error.error_code,
            http_status_code=error.status_code,
        )
    if isinstance(error, ValidationError):
        return AlertFailurePolicy(retryable=False, error_code="alert_payload_invalid")
    if isinstance(error, AlertProviderError):
        return AlertFailurePolicy(
            retryable=False,
            error_code=error.error_code,
            http_status_code=error.status_code,
        )
    return AlertFailurePolicy(retryable=True, error_code="alert_unexpected_error")


class AlertWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sender: AlertSender,
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
        self._sender = sender
        self.worker_id = worker_id
        self.lease_duration = lease_duration
        self.retry_delay = retry_delay
        self.max_retry_delay = max_retry_delay
        self._jitter_source = jitter_source
        self.poll_interval_seconds = poll_interval_seconds

    async def reserve_next_job(self) -> ReservedAlertJob | None:
        async with self._session_factory() as session, session.begin():
            now = await self._database_now(session)
            await self._recover_expired_leases(session, now)

            job = await session.scalar(
                select(ProcessingJob)
                .where(
                    ProcessingJob.job_type == JobType.ALERT_DELIVERY,
                    ProcessingJob.classification_id.is_not(None),
                    ProcessingJob.status.in_((JobStatus.PENDING, JobStatus.RETRY_SCHEDULED)),
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
            if job is None or job.classification_id is None:
                return None

            job.status = JobStatus.PROCESSING
            job.attempt_count += 1
            job.locked_at = now
            job.locked_by = self.worker_id
            job.updated_at = now

            delivery = await self._prepare_attempt(session, job, now)
            return ReservedAlertJob(
                id=job.id,
                ticket_id=job.ticket_id,
                classification_id=job.classification_id,
                alert_delivery_id=delivery.id,
                attempt_count=job.attempt_count,
                request_payload=dict(delivery.request_payload),
            )

    async def run_once(self) -> bool:
        reserved_job = await self.reserve_next_job()
        if reserved_job is None:
            return False

        log_event(
            logger,
            logging.INFO,
            "alert_attempt",
            job_id=reserved_job.id,
            ticket_id=reserved_job.ticket_id,
            classification_id=reserved_job.classification_id,
            alert_id=reserved_job.alert_delivery_id,
            attempt_number=reserved_job.attempt_count,
            worker_id=self.worker_id,
        )

        try:
            payload = AlertPayload.model_validate(reserved_job.request_payload)
            result = await self._sender.deliver(payload)
        except Exception as error:
            recorded = await self._record_failure(reserved_job, error)
            if recorded:
                if recorded.status == JobStatus.RETRY_SCHEDULED:
                    ALERTS_RETRY.inc()
                    event = "alert_retry"
                else:
                    event = "alert_failed"
                log_event(
                    logger,
                    logging.WARNING,
                    event,
                    job_id=reserved_job.id,
                    ticket_id=reserved_job.ticket_id,
                    classification_id=reserved_job.classification_id,
                    alert_id=reserved_job.alert_delivery_id,
                    attempt_number=reserved_job.attempt_count,
                    worker_id=self.worker_id,
                    error_code=recorded.error_code,
                    next_retry_at=recorded.next_retry_at,
                )
            return True

        completed = await self._complete_job(reserved_job, result)
        if completed is not None:
            ALERTS_DELIVERED.inc()
            URGENT_ALERT_END_TO_END.observe(completed.end_to_end_seconds)
            if completed.end_to_end_seconds > URGENT_ALERT_SLA_THRESHOLD_SECONDS:
                URGENT_ALERT_SLA_VIOLATIONS.inc()
            log_event(
                logger,
                logging.INFO,
                "alert_delivered",
                job_id=reserved_job.id,
                ticket_id=reserved_job.ticket_id,
                classification_id=reserved_job.classification_id,
                alert_id=reserved_job.alert_delivery_id,
                attempt_number=reserved_job.attempt_count,
                provider_request_id=result.provider_alert_id,
                worker_id=self.worker_id,
            )
        else:
            log_event(
                logger,
                logging.INFO,
                "alert_lease_lost",
                job_id=reserved_job.id,
                ticket_id=reserved_job.ticket_id,
                classification_id=reserved_job.classification_id,
                alert_id=reserved_job.alert_delivery_id,
                attempt_number=reserved_job.attempt_count,
                worker_id=self.worker_id,
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

    async def _prepare_attempt(
        self,
        session: AsyncSession,
        job: ProcessingJob,
        now: datetime,
    ) -> AlertDelivery:
        if job.classification_id is None:  # pragma: no cover - guarded by reservation query
            raise RuntimeError("alert job has no classification")

        delivery = await session.scalar(
            select(AlertDelivery)
            .where(
                AlertDelivery.classification_id == job.classification_id,
                AlertDelivery.attempt_number == job.attempt_count,
            )
            .with_for_update()
        )
        if delivery is None:
            first_delivery = await session.scalar(
                select(AlertDelivery)
                .where(AlertDelivery.classification_id == job.classification_id)
                .order_by(AlertDelivery.attempt_number)
                .limit(1)
            )
            if first_delivery is None:
                raise RuntimeError("alert delivery payload was not found")
            delivery = AlertDelivery(
                classification_id=job.classification_id,
                attempt_number=job.attempt_count,
                status=AlertDeliveryStatus.PENDING,
                request_payload=dict(first_delivery.request_payload),
            )
            session.add(delivery)

        delivery.status = AlertDeliveryStatus.PENDING
        delivery.attempted_at = now
        delivery.next_retry_at = None
        delivery.updated_at = now
        await session.flush()
        return delivery

    async def _complete_job(
        self,
        reserved_job: ReservedAlertJob,
        result: AlertProviderResult,
    ) -> CompletedAlert | None:
        async with self._session_factory() as session, session.begin():
            job = await self._owned_job(session, reserved_job)
            if job is None:
                return None
            delivery = await session.get(
                AlertDelivery,
                reserved_job.alert_delivery_id,
                with_for_update=True,
            )
            if delivery is None:
                return None
            ticket = await session.get(Ticket, reserved_job.ticket_id)
            if ticket is None:  # pragma: no cover - protected by the job foreign key
                raise RuntimeError("alert job ticket was not found")

            now = await self._database_now(session)
            delivery.status = AlertDeliveryStatus.DELIVERED
            delivery.provider_request_id = result.provider_alert_id
            delivery.http_status_code = httpx.codes.ACCEPTED
            delivery.response_body_excerpt = result.response_body_excerpt
            delivery.error_code = None
            delivery.error_message = None
            delivery.next_retry_at = None
            delivery.delivered_at = now
            delivery.updated_at = now

            job.status = JobStatus.COMPLETED
            job.locked_at = None
            job.locked_by = None
            job.last_error_code = None
            job.last_error_message = None
            job.completed_at = now
            job.updated_at = now
            return CompletedAlert(
                end_to_end_seconds=(now - ticket.received_at).total_seconds()
            )

    async def _record_failure(
        self,
        reserved_job: ReservedAlertJob,
        error: Exception,
    ) -> RecordedAlertFailure | None:
        async with self._session_factory() as session, session.begin():
            job = await self._owned_job(session, reserved_job)
            if job is None:
                return None
            delivery = await session.get(
                AlertDelivery,
                reserved_job.alert_delivery_id,
                with_for_update=True,
            )
            if delivery is None:
                return None

            now = await self._database_now(session)
            policy = _failure_policy(error)
            job.locked_at = None
            job.locked_by = None
            job.last_error_code = policy.error_code
            job.last_error_message = (
                "retryable alert delivery failure"
                if policy.retryable
                else "permanent alert delivery failure"
            )
            job.updated_at = now

            delivery.status = (
                AlertDeliveryStatus.TRANSIENT_FAILURE
                if policy.retryable
                else AlertDeliveryStatus.PERMANENT_FAILURE
            )
            delivery.http_status_code = policy.http_status_code
            delivery.error_code = policy.error_code
            delivery.error_message = job.last_error_message
            delivery.updated_at = now

            if policy.retryable and job.attempt_count < job.max_attempts:
                available_at = now + self._retry_delay_for(job.attempt_count)
                job.status = JobStatus.RETRY_SCHEDULED
                job.available_at = available_at
                delivery.next_retry_at = available_at
            else:
                job.status = JobStatus.FAILED
                job.completed_at = now
                delivery.next_retry_at = None
            return RecordedAlertFailure(
                status=job.status,
                error_code=policy.error_code,
                next_retry_at=delivery.next_retry_at,
            )

    async def _recover_expired_leases(self, session: AsyncSession, now: datetime) -> None:
        expired_jobs = list(
            (
                await session.scalars(
                    select(ProcessingJob)
                    .where(
                        ProcessingJob.job_type == JobType.ALERT_DELIVERY,
                        ProcessingJob.status == JobStatus.PROCESSING,
                        or_(
                            ProcessingJob.locked_at.is_(None),
                            ProcessingJob.locked_at <= now - self.lease_duration,
                        ),
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for job in expired_jobs:
            delivery = None
            if job.classification_id is not None:
                delivery = await session.scalar(
                    select(AlertDelivery)
                    .where(
                        AlertDelivery.classification_id == job.classification_id,
                        AlertDelivery.attempt_number == job.attempt_count,
                    )
                    .with_for_update()
                )

            exhausted = job.attempt_count >= job.max_attempts
            job.status = JobStatus.FAILED if exhausted else JobStatus.RETRY_SCHEDULED
            job.available_at = now
            job.locked_at = None
            job.locked_by = None
            job.last_error_code = "alert_lease_exhausted" if exhausted else "alert_lease_expired"
            job.last_error_message = "alert worker lease expired before completion"
            job.completed_at = now if exhausted else None
            job.updated_at = now

            if delivery is not None and delivery.status == AlertDeliveryStatus.PENDING:
                delivery.status = (
                    AlertDeliveryStatus.PERMANENT_FAILURE
                    if exhausted
                    else AlertDeliveryStatus.TRANSIENT_FAILURE
                )
                delivery.error_code = job.last_error_code
                delivery.error_message = job.last_error_message
                delivery.next_retry_at = None if exhausted else now
                delivery.updated_at = now

    async def _owned_job(
        self,
        session: AsyncSession,
        reserved_job: ReservedAlertJob,
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

    async def _database_now(self, session: AsyncSession) -> datetime:
        now = await session.scalar(select(func.clock_timestamp()))
        if now is None:  # pragma: no cover - PostgreSQL always returns a value
            raise RuntimeError("database clock is unavailable")
        return datetime.fromtimestamp(now.timestamp(), tz=now.tzinfo)

    def _retry_delay_for(self, attempt_count: int) -> timedelta:
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
        return timedelta(
            seconds=exponential_seconds
            + min(max(0.0, jitter_seconds), jitter_ceiling)
        )


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"


async def _run_worker() -> None:
    settings = get_settings()
    configure_structured_logging(settings.log_level)
    start_worker_metrics_server(settings.metrics_port)
    try:
        async with httpx.AsyncClient() as http_client:
            worker = AlertWorker(
                async_session_factory,
                PlantaoClient(
                    base_url=settings.plantao_base_url,
                    timeout_seconds=settings.plantao_timeout_seconds,
                    http_client=http_client,
                ),
                worker_id=_worker_id(),
                lease_duration=timedelta(seconds=settings.alert_worker_lease_seconds),
                retry_delay=timedelta(seconds=settings.alert_worker_retry_delay_seconds),
                max_retry_delay=timedelta(
                    seconds=settings.alert_worker_max_retry_delay_seconds
                ),
                poll_interval_seconds=settings.alert_worker_poll_interval_seconds,
            )
            await worker.run_forever()
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
