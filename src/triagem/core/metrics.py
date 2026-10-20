from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
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


async def metrics_endpoint(_: Request) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def start_worker_metrics_server(port: int) -> None:
    start_http_server(port)
