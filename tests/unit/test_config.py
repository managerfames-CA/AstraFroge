"""Settings validation tests."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_MUTATION_TOKEN = "m" * 32


def test_wildcard_cors_origin_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Wildcard CORS origins"):
        Settings(_env_file=None, cors_origins=["*"])


def test_invalid_cors_origin_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Invalid CORS origin"):
        Settings(_env_file=None, cors_origins=["localhost:5173"])


def test_execution_requires_mutation_token() -> None:
    with pytest.raises(
        ValidationError,
        match="Demo execution requires a configured mutation API token",
    ):
        Settings(
            _env_file=None,
            execution_enabled=True,
            binance_demo_base_url="https://demo-fapi.binance.com",
            binance_demo_api_key="demo-key",
            binance_demo_api_secret="demo-secret",
        )


def test_execution_requires_demo_unlock_configuration() -> None:
    with pytest.raises(
        ValidationError,
        match="Demo execution requires configured Binance Demo credentials",
    ):
        Settings(
            _env_file=None,
            execution_enabled=True,
            mutation_api_token=_MUTATION_TOKEN,
        )


def test_execution_rejects_live_host() -> None:
    with pytest.raises(
        ValidationError,
        match="official USD-M Futures Demo endpoint",
    ):
        Settings(
            _env_file=None,
            execution_enabled=True,
            mutation_api_token=_MUTATION_TOKEN,
            binance_demo_base_url="https://fapi.binance.com",
            binance_demo_api_key="demo-key",
            binance_demo_api_secret="demo-secret",
        )


def test_execution_rejects_unapproved_demo_like_host() -> None:
    with pytest.raises(
        ValidationError,
        match="official USD-M Futures Demo endpoint",
    ):
        Settings(
            _env_file=None,
            execution_enabled=True,
            mutation_api_token=_MUTATION_TOKEN,
            binance_demo_base_url="https://demo-fapi.binance.example",
            binance_demo_api_key="demo-key",
            binance_demo_api_secret="demo-secret",
        )


def test_partial_demo_credentials_fail_closed() -> None:
    with pytest.raises(ValidationError, match="configured together"):
        Settings(_env_file=None, binance_demo_api_key="key-only")


def test_known_secret_values_are_not_serialized_as_plain_text() -> None:
    settings = Settings(
        _env_file=None,
        binance_demo_api_key="demo-key",
        binance_demo_api_secret="demo-secret",
    )

    dumped = settings.model_dump_json()
    assert "demo-key" not in dumped
    assert "demo-secret" not in dumped
    assert settings.known_secret_values == ("demo-key", "demo-secret")


def test_execution_can_be_enabled_for_official_demo_endpoint() -> None:
    settings = Settings(
        _env_file=None,
        execution_enabled=True,
        mutation_api_token=_MUTATION_TOKEN,
        binance_demo_base_url="https://demo-fapi.binance.com",
        binance_demo_api_key="demo-key",
        binance_demo_api_secret="demo-secret",
    )

    assert settings.execution_enabled is True
    assert settings.binance_demo_base_url == "https://demo-fapi.binance.com"


def test_scanner_auto_start_defaults_off_in_test_environment() -> None:
    settings = Settings(_env_file=None, environment="test")

    assert settings.scanner_auto_start is False


def test_scanner_auto_start_can_be_explicitly_enabled_in_test_environment() -> None:
    settings = Settings(_env_file=None, environment="test", scanner_auto_start=True)

    assert settings.scanner_auto_start is True
