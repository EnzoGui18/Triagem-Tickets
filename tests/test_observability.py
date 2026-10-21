from __future__ import annotations

import json
import logging
from io import StringIO
from uuid import uuid4

from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from triagem.core.metrics import TICKETS_RECEIVED
from triagem.core.structured_logging import (
    JsonLogFormatter,
    log_event,
    reset_request_id,
    set_request_id,
)


def test_correlation_id_is_preserved_on_response(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": "request-safe-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "request-safe-123"


def test_hostile_correlation_id_is_replaced(client: TestClient) -> None:
    response = client.get(
        "/health/live",
        headers={"X-Request-ID": "invalid\nAuthorization: Bearer secret"},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "invalid\nAuthorization: Bearer secret"
    assert len(response.headers["X-Request-ID"]) == 36


def test_structured_log_contains_correlation_and_operational_ids() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger(f"test-observability-{uuid4()}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    token = set_request_id("request-structured")
    try:
        log_event(
            logger,
            logging.INFO,
            "ticket_received",
            external_id="ticket-123",
            job_id=uuid4(),
            customer_email="private@example.com",
            authorization="Bearer secret",
        )
    finally:
        reset_request_id(token)

    payload = json.loads(stream.getvalue())
    assert payload["event"] == "ticket_received"
    assert payload["request_id"] == "request-structured"
    assert payload["external_id"] == "ticket-123"
    assert "authorization" not in payload
    assert "customer_email" not in payload
    assert "private@example.com" not in stream.getvalue()
    assert "Bearer secret" not in stream.getvalue()


def test_metrics_endpoint_exposes_prioritized_counters(client: TestClient) -> None:
    before = generate_latest()
    TICKETS_RECEIVED.inc()

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    for metric_name in (
        b"tickets_received_total",
        b"tickets_duplicate_total",
        b"classification_completed_total",
        b"classification_retry_total",
        b"classification_failed_total",
        b"classification_queue_depth",
        b"llm_requests_total",
        b"llm_rate_limited_total",
        b"llm_errors_total",
        b"llm_input_tokens_total",
        b"llm_output_tokens_total",
        b"llm_cost_usd_total",
        b"alerts_delivered_total",
        b"alerts_retry_total",
        b"ingestion_duration_seconds",
        b"classification_queue_wait_seconds",
        b"llm_request_duration_seconds",
        b"classification_duration_seconds",
        b"alert_request_duration_seconds",
        b"urgent_alert_end_to_end_seconds",
        b"urgent_alert_sla_violations_total",
        b"ticket_processing_duration_seconds",
        b"llm_rate_limit_wait_total",
        b"database_pool_capacity",
        b"database_pool_connections",
        b"database_pool_checked_out",
    ):
        assert metric_name in response.content
    assert generate_latest() != before
