from __future__ import annotations

from typing import Protocol

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)
from starlette.requests import Request
from starlette.responses import Response

TICKETS_RECEIVED = Counter(
    "tickets_received_total",
    "Tickets durably accepted for the first time.",
)
TICKETS_DUPLICATE = Counter(
    "tickets_duplicate_total",
    "Idempotent duplicate ticket requests.",
)
CLASSIFICATION_COMPLETED = Counter(
    "classification_completed_total",
    "Classification jobs completed successfully.",
)
CLASSIFICATION_RETRY = Counter(
    "classification_retry_total",
    "Classification attempts scheduled for retry.",
)
CLASSIFICATION_FAILED = Counter(
    "classification_failed_total",
    "Classification jobs permanently failed.",
)
CLASSIFICATION_QUEUE_DEPTH = Gauge(
    "classification_queue_depth",
    "Classification jobs currently pending or retryable.",
)
LLM_REQUESTS = Counter("llm_requests_total", "HTTP requests started against the LLM provider.")
LLM_RATE_LIMITED = Counter(
    "llm_rate_limited_total",
    "HTTP 429 responses returned by the LLM provider.",
)
LLM_ERRORS = Counter("llm_errors_total", "Failed LLM provider requests or invalid responses.")
LLM_INPUT_TOKENS = Counter("llm_input_tokens_total", "Input tokens reported by the LLM.")
LLM_OUTPUT_TOKENS = Counter("llm_output_tokens_total", "Output tokens reported by the LLM.")
LLM_COST_USD = Counter(
    "llm_cost_usd_total",
    "Observed classification cost in USD; PostgreSQL Decimal values are authoritative.",
)
ALERTS_DELIVERED = Counter("alerts_delivered_total", "Urgent alerts accepted by plantao.")
ALERTS_RETRY = Counter("alerts_retry_total", "Urgent alert deliveries scheduled for retry.")
URGENT_ALERT_SLA_THRESHOLD_SECONDS = 60.0
URGENT_ALERT_SLA_VIOLATIONS = Counter(
    "urgent_alert_sla_violations_total",
    "Urgent alerts delivered more than 60 seconds after ticket receipt.",
)
INGESTION_DURATION = Histogram(
    "ingestion_duration_seconds",
    "End-to-end duration of POST /tickets, including validation and persistence.",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
CLASSIFICATION_QUEUE_WAIT = Histogram(
    "classification_queue_wait_seconds",
    "Time from durable job creation until its first reservation.",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
)
LLM_REQUEST_DURATION = Histogram(
    "llm_request_duration_seconds",
    "Duration of HTTP requests to the LLM provider.",
    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 10),
)
CLASSIFICATION_DURATION = Histogram(
    "classification_duration_seconds",
    "Duration of a classification attempt after reservation.",
    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60),
)
ALERT_REQUEST_DURATION = Histogram(
    "alert_request_duration_seconds",
    "Duration of HTTP requests to the on-call provider.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
URGENT_ALERT_END_TO_END = Histogram(
    "urgent_alert_end_to_end_seconds",
    "Time from durable ticket receipt until the urgent alert is delivered.",
    buckets=(1, 5, 10, 15, 30, 45, 60, 90, 120, 300, 600),
)
TOTAL_PROCESSING_DURATION = Histogram(
    "ticket_processing_duration_seconds",
    "Time from durable classification job creation until completion.",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
)
LLM_RATE_LIMIT_WAITS = Counter(
    "llm_rate_limit_wait_total",
    "Local waits imposed by the distributed LLM rate limiter.",
)
DATABASE_POOL_CAPACITY = Gauge(
    "database_pool_capacity",
    "Configured SQLAlchemy connection pool capacity, including overflow.",
)
DATABASE_POOL_CONNECTIONS = Gauge(
    "database_pool_connections",
    "Connections currently opened by this process.",
)
DATABASE_POOL_CHECKED_OUT = Gauge(
    "database_pool_checked_out",
    "Database connections currently checked out by this process.",
)


class ObservablePool(Protocol):
    def checkedin(self) -> int: ...

    def checkedout(self) -> int: ...


def register_database_pool_metrics(pool: ObservablePool, *, capacity: int) -> None:
    """Bind process-local pool gauges without creating database traffic."""

    checked_in = pool.checkedin
    checked_out = pool.checkedout
    DATABASE_POOL_CAPACITY.set(capacity)
    DATABASE_POOL_CONNECTIONS.set_function(lambda: checked_in() + checked_out())
    DATABASE_POOL_CHECKED_OUT.set_function(checked_out)


async def metrics_endpoint(_: Request) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def start_worker_metrics_server(port: int) -> None:
    start_http_server(port)
