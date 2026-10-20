from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from triagem.models import (
    AlertDelivery,
    AlertDeliveryStatus,
    Classification,
    ClassificationAttempt,
    JobStatus,
    JobType,
    ProcessingJob,
    Ticket,
)
from triagem.schemas import (
    TicketTimelineEventResponse,
    TicketTimelineEventType,
    TicketTimelineResponse,
)

_EVENT_ORDER = {event_type: index for index, event_type in enumerate(TicketTimelineEventType)}


def _event(
    event_type: TicketTimelineEventType,
    occurred_at: datetime,
    **details: Any,
) -> TicketTimelineEventResponse:
    return TicketTimelineEventResponse(
        event_type=event_type,
        occurred_at=occurred_at,
        **details,
    )


def _sort_events(
    events: Iterable[TicketTimelineEventResponse],
) -> list[TicketTimelineEventResponse]:
    return sorted(
        events,
        key=lambda event: (
            event.occurred_at,
            _EVENT_ORDER[event.event_type],
            str(event.job_id or event.classification_id or event.alert_id or UUID(int=0)),
            event.attempt_number or 0,
        ),
    )


async def get_ticket_timeline(
    session: AsyncSession,
    external_id: str,
) -> TicketTimelineResponse | None:
    ticket = await session.scalar(select(Ticket).where(Ticket.external_id == external_id))
    if ticket is None:
        return None

    jobs = list(
        (
            await session.scalars(
                select(ProcessingJob)
                .where(ProcessingJob.ticket_id == ticket.id)
                .order_by(ProcessingJob.created_at, ProcessingJob.id)
            )
        ).all()
    )
    classification_jobs = [job for job in jobs if job.job_type == JobType.CLASSIFICATION]
    classification_job_ids = [job.id for job in classification_jobs]
    attempts = (
        list(
            (
                await session.scalars(
                    select(ClassificationAttempt)
                    .where(ClassificationAttempt.processing_job_id.in_(classification_job_ids))
                    .order_by(
                        ClassificationAttempt.started_at,
                        ClassificationAttempt.attempt_number,
                    )
                )
            ).all()
        )
        if classification_job_ids
        else []
    )
    classifications = list(
        (
            await session.scalars(
                select(Classification)
                .where(Classification.ticket_id == ticket.id)
                .order_by(Classification.created_at, Classification.id)
            )
        ).all()
    )
    classification_ids = [classification.id for classification in classifications]
    alert_deliveries = (
        list(
            (
                await session.scalars(
                    select(AlertDelivery)
                    .where(AlertDelivery.classification_id.in_(classification_ids))
                    .order_by(AlertDelivery.created_at, AlertDelivery.attempt_number)
                )
            ).all()
        )
        if classification_ids
        else []
    )

    classification_by_id = {classification.id: classification for classification in classifications}
    job_by_id = {job.id: job for job in classification_jobs}
    attempts_by_job: dict[UUID, list[ClassificationAttempt]] = {}
    for attempt in attempts:
        attempts_by_job.setdefault(attempt.processing_job_id, []).append(attempt)
    alert_job_by_classification: dict[UUID, ProcessingJob] = {}
    for job in jobs:
        if job.job_type == JobType.ALERT_DELIVERY and job.classification_id is not None:
            alert_job_by_classification[job.classification_id] = job

    events = [
        _event(TicketTimelineEventType.TICKET_RECEIVED, ticket.received_at),
    ]
    for job in classification_jobs:
        events.append(
            _event(
                TicketTimelineEventType.CLASSIFICATION_QUEUED,
                job.created_at,
                job_id=job.id,
                status=JobStatus.PENDING.value,
            )
        )

    for attempt in attempts:
        job = job_by_id[attempt.processing_job_id]
        events.append(
            _event(
                TicketTimelineEventType.CLASSIFICATION_STARTED,
                attempt.started_at,
                job_id=job.id,
                attempt_number=attempt.attempt_number,
                status=JobStatus.PROCESSING.value,
            )
        )
        if attempt.finished_at is None:
            continue
        if attempt.status == JobStatus.COMPLETED:
            classification = (
                classification_by_id.get(job.classification_id)
                if job.classification_id is not None
                else None
            )
            events.append(
                _event(
                    TicketTimelineEventType.CLASSIFICATION_COMPLETED,
                    attempt.finished_at,
                    job_id=job.id,
                    classification_id=classification.id if classification else None,
                    attempt_number=attempt.attempt_number,
                    status=attempt.status.value,
                    category=classification.category if classification else None,
                    priority=classification.priority if classification else None,
                    provider_request_id=attempt.provider_request_id,
                )
            )
        else:
            event_type = (
                TicketTimelineEventType.CLASSIFICATION_RETRY
                if attempt.status == JobStatus.RETRY_SCHEDULED
                else TicketTimelineEventType.CLASSIFICATION_FAILED
            )
            events.append(
                _event(
                    event_type,
                    attempt.finished_at,
                    job_id=job.id,
                    attempt_number=attempt.attempt_number,
                    status=attempt.status.value,
                    error_code=attempt.error_code,
                    error_message=attempt.error_message,
                    next_retry_at=attempt.next_retry_at,
                )
            )

    # Jobs concluded before classification_attempts existed remain explainable.
    for job in classification_jobs:
        job_attempts = attempts_by_job.get(job.id, [])
        if not job_attempts and job.locked_at is not None:
            events.append(
                _event(
                    TicketTimelineEventType.CLASSIFICATION_STARTED,
                    job.locked_at,
                    job_id=job.id,
                    attempt_number=job.attempt_count or None,
                    status=JobStatus.PROCESSING.value,
                )
            )
        if any(attempt.status == JobStatus.COMPLETED for attempt in job_attempts):
            continue
        classification = (
            classification_by_id.get(job.classification_id)
            if job.classification_id is not None
            else None
        )
        if classification is not None:
            events.append(
                _event(
                    TicketTimelineEventType.CLASSIFICATION_COMPLETED,
                    classification.created_at,
                    job_id=job.id,
                    classification_id=classification.id,
                    status=JobStatus.COMPLETED.value,
                    category=classification.category,
                    priority=classification.priority,
                    provider_request_id=classification.provider_request_id,
                )
            )
        elif not job_attempts and job.status in (JobStatus.RETRY_SCHEDULED, JobStatus.FAILED):
            events.append(
                _event(
                    TicketTimelineEventType.CLASSIFICATION_RETRY
                    if job.status == JobStatus.RETRY_SCHEDULED
                    else TicketTimelineEventType.CLASSIFICATION_FAILED,
                    job.updated_at,
                    job_id=job.id,
                    attempt_number=job.attempt_count or None,
                    status=job.status.value,
                    error_code=job.last_error_code,
                    error_message=job.last_error_message,
                    next_retry_at=(
                        job.available_at if job.status == JobStatus.RETRY_SCHEDULED else None
                    ),
                )
            )

    for delivery in alert_deliveries:
        classification = classification_by_id[delivery.classification_id]
        alert_job = alert_job_by_classification.get(delivery.classification_id)
        if delivery.attempt_number == 1:
            events.append(
                _event(
                    TicketTimelineEventType.ALERT_CREATED,
                    classification.created_at,
                    job_id=alert_job.id if alert_job else None,
                    classification_id=classification.id,
                    alert_id=delivery.id,
                    status=AlertDeliveryStatus.PENDING.value,
                    category=classification.category,
                    priority=classification.priority,
                )
            )
        if delivery.attempted_at is None:
            continue
        events.append(
            _event(
                TicketTimelineEventType.ALERT_ATTEMPT,
                delivery.attempted_at,
                job_id=alert_job.id if alert_job else None,
                classification_id=classification.id,
                alert_id=delivery.id,
                attempt_number=delivery.attempt_number,
                status=AlertDeliveryStatus.PENDING.value,
            )
        )
        common_details = {
            "job_id": alert_job.id if alert_job else None,
            "classification_id": classification.id,
            "alert_id": delivery.id,
            "attempt_number": delivery.attempt_number,
            "status": delivery.status.value,
            "http_status_code": delivery.http_status_code,
            "provider_request_id": delivery.provider_request_id,
        }
        if delivery.status == AlertDeliveryStatus.DELIVERED and delivery.delivered_at is not None:
            events.append(
                _event(
                    TicketTimelineEventType.ALERT_DELIVERED,
                    delivery.delivered_at,
                    **common_details,
                )
            )
        elif delivery.status in (
            AlertDeliveryStatus.TRANSIENT_FAILURE,
            AlertDeliveryStatus.PERMANENT_FAILURE,
        ):
            events.append(
                _event(
                    TicketTimelineEventType.ALERT_RETRY
                    if delivery.status == AlertDeliveryStatus.TRANSIENT_FAILURE
                    else TicketTimelineEventType.ALERT_FAILED,
                    delivery.updated_at,
                    error_code=delivery.error_code,
                    error_message=delivery.error_message,
                    next_retry_at=delivery.next_retry_at,
                    **common_details,
                )
            )

    return TicketTimelineResponse(
        external_id=ticket.external_id,
        events=_sort_events(events),
    )
