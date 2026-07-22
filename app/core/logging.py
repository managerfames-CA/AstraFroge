"""Structured logging and secret redaction."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings

REDACTED = "[REDACTED]"
SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "api_secret",
    "secret",
    "token",
    "authorization",
    "signature",
    "password",
}
_AUDIT_FIELDS = (
    "audit_event",
    "actor",
    "action",
    "resource",
    "outcome",
    "status_code",
    "idempotency_key_hash",
    "client_ip",
)

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|api[_-]?secret|secret|token|authorization|signature|password)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*")


def redact_text(value: str, known_secrets: Iterable[str] = ()) -> str:
    """Redact common credential patterns and configured secret values from text."""

    redacted = _BEARER_TOKEN.sub(f"Bearer {REDACTED}", value)
    redacted = _SENSITIVE_ASSIGNMENT.sub(rf"\1\2{REDACTED}", redacted)
    for secret in known_secrets:
        if secret:
            redacted = redacted.replace(secret, REDACTED)
    return redacted


def redact_value(value: Any, known_secrets: Iterable[str] = ()) -> Any:
    """Recursively redact sensitive keys and string values."""

    if isinstance(value, Mapping):
        return {
            key: REDACTED
            if str(key).lower() in SENSITIVE_KEYS
            else redact_value(item, known_secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item, known_secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, known_secrets) for item in value)
    if isinstance(value, str):
        return redact_text(value, known_secrets)
    return value


class SecretRedactionFilter(logging.Filter):
    """Redact secrets from log records before formatting."""

    def __init__(self, known_secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._known_secrets = tuple(secret for secret in known_secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_value(record.msg, self._known_secrets)
        if isinstance(record.args, Mapping):
            record.args = redact_value(record.args, self._known_secrets)
        elif isinstance(record.args, tuple):
            record.args = tuple(redact_value(item, self._known_secrets) for item in record.args)
        return True


class JsonFormatter(logging.Formatter):
    """Format log records as one redacted JSON object per line."""

    def __init__(self, known_secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._known_secrets = tuple(secret for secret in known_secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage(), self._known_secrets),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        for field in _AUDIT_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = redact_value(value, self._known_secrets)
        if record.exc_info:
            payload["exception"] = redact_text(
                self.formatException(record.exc_info), self._known_secrets
            )
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(settings: Settings) -> None:
    """Configure root logging with JSON output and secret redaction."""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(settings.known_secret_values))
    handler.addFilter(SecretRedactionFilter(settings.known_secret_values))

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(settings.log_level)
