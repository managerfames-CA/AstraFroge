"""Secret-safety regression tests for BE-16."""

from app.services.notifications import NotificationService


def test_redacts_message_credentials() -> None:
    message = "api_key=abc123 secret=xyz Bearer token-value"
    cleaned = NotificationService._redact_string(message)
    assert "abc123" not in cleaned
    assert "xyz" not in cleaned
    assert "token-value" not in cleaned


def test_redacts_nested_metadata_credentials() -> None:
    cleaned = NotificationService._redact_secrets(
        {"nested": {"api_secret": "hidden"}, "message": "token=visible"}
    )
    assert cleaned["nested"]["api_secret"] == "[REDACTED]"
    assert "visible" not in cleaned["message"]
