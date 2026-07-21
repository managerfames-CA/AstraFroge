"""Secret-redaction tests."""

import json
import logging

from app.core.logging import JsonFormatter, SecretRedactionFilter, redact_value


def test_recursive_redaction_covers_sensitive_keys_and_tokens() -> None:
    value = {
        "api_key": "abc",
        "nested": {
            "authorization": "Bearer private-token",
            "message": "signature=deadbeef token:xyz password=hunter2",
        },
    }

    redacted = redact_value(value)

    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["authorization"] == "[REDACTED]"
    assert "deadbeef" not in redacted["nested"]["message"]
    assert "xyz" not in redacted["nested"]["message"]
    assert "hunter2" not in redacted["nested"]["message"]


def test_log_filter_redacts_configured_secret_and_bearer_token() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="credential demo-secret Authorization: Bearer opaque-token",
        args=(),
        exc_info=None,
    )
    filter_ = SecretRedactionFilter(["demo-secret"])

    assert filter_.filter(record) is True
    payload = json.loads(JsonFormatter(["demo-secret"]).format(record))
    assert "demo-secret" not in payload["message"]
    assert "opaque-token" not in payload["message"]
    assert "[REDACTED]" in payload["message"]


def test_exception_text_is_redacted_by_formatter() -> None:
    try:
        raise RuntimeError("demo-secret must not leak")
    except RuntimeError:
        import sys

        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Unhandled error",
        args=(),
        exc_info=exc_info,
    )
    payload = json.loads(JsonFormatter(["demo-secret"]).format(record))

    assert "demo-secret" not in payload["exception"]
    assert "[REDACTED]" in payload["exception"]
