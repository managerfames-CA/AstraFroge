"""Tests for secret-safe Binance Demo account diagnostics."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, cast

from app.api.v1.routes.execution import execution_account_diagnostic
from app.core.config import Settings
from app.integrations.binance.private_demo_client import (
    BinanceDemoPrivateClient,
    BinanceDemoPrivateClientError,
)
from app.schemas.execution import DemoAccountDiagnosticResponse


class HealthyDemoClient:
    """Minimal fake private client returning a successful account payload."""

    def account(self) -> dict[str, object]:
        return {"canTrade": True}


class FailingDemoClient:
    """Minimal fake private client returning a sanitized Binance adapter error."""

    def account(self) -> dict[str, object]:
        raise BinanceDemoPrivateClientError(
            "Binance demo private API request failed with status 401 and exchange code -2015",
            status_code=401,
            exchange_code=-2015,
        )


def _run[T](awaitable: Awaitable[T]) -> T:
    return cast(T, asyncio.run(cast(Any, awaitable)))


def _configured_settings() -> Settings:
    return Settings(
        binance_demo_base_url="https://demo-fapi.binance.com",
        binance_demo_api_key="dummy-key",
        binance_demo_api_secret="dummy-secret",
    )


def test_demo_account_diagnostic_reports_missing_configuration() -> None:
    result = _run(execution_account_diagnostic(settings=Settings(), client=None))

    assert isinstance(result, DemoAccountDiagnosticResponse)
    assert result.diagnostic_status == "CONFIGURATION_LOCKED"
    assert result.demo_base_url_configured is False
    assert result.demo_api_key_configured is False
    assert result.demo_api_secret_configured is False
    assert result.demo_credentials_configured is False
    assert result.private_client_available is False
    assert result.execution_enabled is False
    assert result.account_endpoint_status == "NOT_TESTED"
    assert result.account_error_code == (
        "DEMO_BASE_URL_MISSING+DEMO_API_KEY_MISSING+DEMO_API_SECRET_MISSING"
    )


def test_demo_account_diagnostic_reports_connected_account() -> None:
    result = _run(
        execution_account_diagnostic(
            settings=_configured_settings(),
            client=cast(BinanceDemoPrivateClient, HealthyDemoClient()),
        )
    )

    assert result.diagnostic_status == "CONNECTED"
    assert result.demo_base_url_configured is True
    assert result.demo_base_url_host == "demo-fapi.binance.com"
    assert result.demo_api_key_configured is True
    assert result.demo_api_secret_configured is True
    assert result.demo_credentials_configured is True
    assert result.private_client_available is True
    assert result.account_endpoint_status == "CONNECTED"
    assert result.account_can_trade is True
    assert result.account_error_code is None


def test_demo_account_diagnostic_reports_private_api_error() -> None:
    result = _run(
        execution_account_diagnostic(
            settings=_configured_settings(),
            client=cast(BinanceDemoPrivateClient, FailingDemoClient()),
        )
    )

    assert result.diagnostic_status == "ACCOUNT_API_ERROR"
    assert result.account_endpoint_status == "ERROR"
    assert result.account_error_code == "DEMO_PRIVATE_ACCOUNT_REQUEST_FAILED"
    assert result.account_error_status_code == 401
    assert result.account_exchange_code == -2015
    assert "status 401" in (result.account_error_message or "")
