from __future__ import annotations

import hashlib
import json
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from triagem.models import JobType, ProcessingJob, Ticket
from triagem.schemas import TicketPayload


class TicketPayloadConflictError(Exception):
    pass


def calculate_payload_hash(payload: TicketPayload) -> str:
    canonical_payload = payload.model_dump(mode="json", by_alias=True)
    canonical_payload["criado_em"] = payload.source_created_at.astimezone(UTC).isoformat()
    encoded_payload = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded_payload).hexdigest()


async def ingest_ticket(session: AsyncSession, payload: TicketPayload) -> Ticket:
    payload_hash = calculate_payload_hash(payload)
    ticket_values = {
        "external_id": payload.external_id,
        "customer_external_id": payload.customer.external_id,
        "customer_name": payload.customer.name,
        "customer_email": str(payload.customer.email),
        "subject": payload.subject,
        "message": payload.message,
        "channel": payload.channel,
        "source_created_at": payload.source_created_at,
        "payload_hash": payload_hash,
    }

    async with session.begin():
        insert_result = await session.execute(
            insert(Ticket)
            .values(**ticket_values)
            .on_conflict_do_nothing(constraint="uq_tickets_external_id")
            .returning(Ticket)
        )
        ticket = insert_result.scalar_one_or_none()

        if ticket is None:
            ticket = await session.scalar(
                select(Ticket).where(Ticket.external_id == payload.external_id)
            )
            if ticket is None:
                raise RuntimeError("conflicting ticket was not found")
            if ticket.payload_hash != payload_hash:
                raise TicketPayloadConflictError
            return ticket

        session.add(
            ProcessingJob(
                ticket_id=ticket.id,
                job_type=JobType.CLASSIFICATION,
                queue_priority=0,
            )
        )

    return ticket
