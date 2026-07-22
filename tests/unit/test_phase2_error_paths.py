"""Phase 2 fail-closed and adapter error-path coverage."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.integrations.binance.pooled_clients import (
    PooledBinanceDemoRecoveryClient,
    PooledBinancePublicClient,
)
from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.integrations.binance.public_client import BinancePublicClientError
from app.services.account_snapshot import AccountSnapshotPayloadError, AccountSnapshotService


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_pooled_public_network_failure_retries_then_fails_unavailable() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout", request=request)

    async def scenario() -> None:
        client = PooledBinancePublicClient(
            base_url="https://example.test",
            timeout_seconds=1,
            retry_attempts=2,
            retry_base_delay_seconds=0,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(BinancePublicClientError, match="unavailable"):
            await client.exchange_time()
        await client.aclose()

    _run(scenario())
    assert calls == 2


def test_pooled_public_rate_limit_without_retry_after_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request, json={"code": -1003})

    async def scenario() -> None:
        client = PooledBinancePublicClient(
            base_url="https://example.test",
            timeout_seconds=1,
            retry_attempts=2,
            retry_base_delay_seconds=0,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(BinancePublicClientError, match="rate limited"):
            await client.exchange_time()
        await client.aclose()

    _run(scenario())


def test_pooled_public_retryable_server_error_can_recover() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, request=request, json={"code": -1})
        return httpx.Response(200, request=request, json={"serverTime": 1_700_000_000_000})

    async def scenario() -> None:
        client = PooledBinancePublicClient(
            base_url="https://example.test",
            timeout_seconds=1,
            retry_attempts=2,
            retry_base_delay_seconds=0,
            transport=httpx.MockTransport(handler),
        )
        payload, _ = await client.exchange_time()
        assert payload["serverTime"] == 1_700_000_000_000
        await client.aclose()

    _run(scenario())
    assert calls == 2


def test_pooled_public_invalid_json_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"not-json")

    async def scenario() -> None:
        client = PooledBinancePublicClient(
            base_url="https://example.test",
            timeout_seconds=1,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(BinancePublicClientError, match="invalid JSON"):
            await client.exchange_time()
        await client.aclose()

    _run(scenario())


def _private_client(handler: Any) -> PooledBinanceDemoRecoveryClient:
    return PooledBinanceDemoRecoveryClient(
        base_url="https://demo.example.test",
        api_key="key",
        api_secret="secret",
        timeout_seconds=1,
        recv_window_ms=5000,
        transport=httpx.MockTransport(handler),
    )


def test_pooled_private_network_failure_is_mapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    client = _private_client(handler)
    with pytest.raises(BinanceDemoPrivateClientError, match="unavailable"):
        client.account()
    client.close()


def test_pooled_private_non_json_http_error_preserves_status_without_exchange_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request, content=b"bad gateway")

    client = _private_client(handler)
    with pytest.raises(BinanceDemoPrivateClientError) as exc_info:
        client.account()
    assert exc_info.value.status_code == 503
    assert exc_info.value.exchange_code is None
    client.close()


def test_pooled_private_invalid_success_json_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"not-json")

    client = _private_client(handler)
    with pytest.raises(BinanceDemoPrivateClientError, match="invalid JSON"):
        client.account()
    client.close()


class PayloadFake:
    def __init__(self) -> None:
        self.account_payload: dict[str, Any] = {
            "canTrade": True,
            "totalWalletBalance": "1000",
            "availableBalance": "800",
            "totalUnrealizedProfit": "0",
            "totalInitialMargin": "0",
            "assets": [],
        }
        self.position_payload: list[dict[str, Any]] = []
        self.income_payload: list[dict[str, Any]] = []

    def account(self) -> dict[str, Any]:
        return self.account_payload

    def positions(self) -> list[dict[str, Any]]:
        return self.position_payload

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return self.income_payload


def test_account_snapshot_rejects_unbounded_income_window() -> None:
    fake = PayloadFake()
    fake.income_payload = [
        {"incomeType": "REALIZED_PNL", "income": "1", "time": index} for index in range(1000)
    ]
    with pytest.raises(AccountSnapshotPayloadError, match="bounded result"):
        AccountSnapshotService(fake).get()


def test_account_snapshot_rejects_invalid_assets_and_positions() -> None:
    fake = PayloadFake()
    fake.account_payload["assets"] = "invalid"
    with pytest.raises(AccountSnapshotPayloadError, match="assets payload"):
        AccountSnapshotService(fake).get()

    fake.account_payload["assets"] = []
    fake.position_payload = [{"symbol": "BTCUSDT", "positionAmt": "1", "leverage": "0"}]
    with pytest.raises(AccountSnapshotPayloadError, match="leverage"):
        AccountSnapshotService(fake).get()


def test_account_snapshot_rejects_invalid_income_and_decimal_fields() -> None:
    fake = PayloadFake()
    fake.income_payload = [{"incomeType": 123, "income": "1"}]
    with pytest.raises(AccountSnapshotPayloadError, match="Income type"):
        AccountSnapshotService(fake).get()

    fake.income_payload = []
    fake.account_payload["totalWalletBalance"] = "NaN"
    with pytest.raises(AccountSnapshotPayloadError, match="totalWalletBalance"):
        AccountSnapshotService(fake).get()
