from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

_REQUEST_ID = ContextVar[str | None]("request_id", default=None)
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SENSITIVE_FIELD_FRAGMENTS = (
    "api_key",
    "authorization",
    "customer_email",
    "email",
    "message",
    "raw_output",
    "system_prompt",
)


def request_id_from_header(value: str | None) -> str:
    if value is not None and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return str(uuid4())


def set_request_id(value: str) -> Token[str | None]:
    return _REQUEST_ID.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    _REQUEST_ID.reset(token)


def get_request_id() -> str | None:
    return _REQUEST_ID.get()


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return value.value
    return type(value).__name__


def _is_sensitive_field(field_name: str) -> bool:
    normalized = field_name.lower().replace("-", "_")
    return any(fragment in normalized for fragment in _SENSITIVE_FIELD_FRAGMENTS)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event_name", record.getMessage())
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": event,
        }
        request_id = getattr(record, "request_id", None)
        if request_id is not None:
            payload["request_id"] = request_id
        fields: dict[str, object] = getattr(record, "event_fields", {})
        payload.update(
            {
                key: _json_value(value)
                for key, value in fields.items()
                if not _is_sensitive_field(key)
            }
        )
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_structured_logging(level: str) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in root.handlers:
        if getattr(handler, "triagem_structured", False):
            handler.setLevel(level.upper())
            _route_uvicorn_logs_to_root()
            return

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level.upper())
    handler.setFormatter(JsonLogFormatter())
    handler.triagem_structured = True  # type: ignore[attr-defined]
    root.handlers.clear()
    root.addHandler(handler)
    _route_uvicorn_logs_to_root()


def _route_uvicorn_logs_to_root() -> None:
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: Any,
) -> None:
    logger.log(
        level,
        event,
        extra={
            "event_name": event,
            "event_fields": fields,
            "request_id": get_request_id(),
        },
    )
