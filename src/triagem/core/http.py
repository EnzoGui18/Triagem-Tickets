from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from triagem.core.metrics import INGESTION_DURATION
from triagem.core.structured_logging import (
    request_id_from_header,
    reset_request_id,
    set_request_id,
)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        started_at = time.perf_counter()
        request_id = request_id_from_header(request.headers.get("X-Request-ID"))
        token = set_request_id(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            if request.method == "POST" and request.url.path.rstrip("/") == "/tickets":
                INGESTION_DURATION.observe(max(0.0, time.perf_counter() - started_at))
            reset_request_id(token)
