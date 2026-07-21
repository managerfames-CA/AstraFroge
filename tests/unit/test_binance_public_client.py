"""Binance public client retry, rate-limit, and payload tests."""

from __future__ import annotations

import httpx
import pytest

from app.integrations.binance.public_client import BinancePublicClient, BinancePublicClientError


@pytest.mark.anyio
async def test_honors_retry_after_then_succeeds() -> None:
    attempts = 0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, headers={"Retry-After": "1"}, request=request)
        return httpx.Response(200, json={"serverTime": 1_700_000_000_000}, request=request)

    client = BinancePublicClient(
        base_url="https://fapi.binance.com",
        timeout_seconds=1,
        retry_attempts=3,
        retry_base_delay_seconds=0.1,
        rate_limit_max_delay_seconds=5,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )

    payload, _ = await client.exchange_time()

    assert payload["serverTime"] == 1_700_000_000_000
    assert attempts == 3
    assert delays == [1.0, 1.0]


@pytest.mark.anyio
async def test_rate_limit_without_retry_after_fails_closed() -> None:
    attempts = 0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, request=request)

    client = BinancePublicClient(
        base_url="https://fapi.binance.com",
        timeout_seconds=1,
        retry_attempts=3,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )

    with pytest.raises(BinancePublicClientError, match="rate limited"):
        await client.exchange_time()

    assert attempts == 1
    assert delays == []


@pytest.mark.anyio
async def test_invalid_retry_after_fails_closed() -> None:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "invalid"},
            request=request,
        )

    client = BinancePublicClient(
        base_url="https://fapi.binance.com",
        timeout_seconds=1,
        retry_attempts=3,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )

    with pytest.raises(BinancePublicClientError, match="rate limited"):
        await client.exchange_time()

    assert delays == []


@pytest.mark.anyio
async def test_non_retryable_status_fails_immediately() -> None:
    attempts = 0

    async def sleep(_: float) -> None:
        raise AssertionError("sleep must not be called")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, request=request)

    client = BinancePublicClient(
        base_url="https://fapi.binance.com",
        timeout_seconds=1,
        retry_attempts=3,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )

    with pytest.raises(BinancePublicClientError, match="status 400"):
        await client.exchange_time()

    assert attempts == 1


@pytest.mark.anyio
async def test_server_retry_exhaustion_returns_stable_error() -> None:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    client = BinancePublicClient(
        base_url="https://fapi.binance.com",
        timeout_seconds=1,
        retry_attempts=2,
        retry_base_delay_seconds=0.25,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )

    with pytest.raises(BinancePublicClientError, match="market data is unavailable"):
        await client.exchange_time()

    assert delays == [0.25]


@pytest.mark.anyio
async def test_network_timeout_retries_then_succeeds() -> None:
    attempts = 0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(200, json={"serverTime": 1_700_000_000_000}, request=request)

    client = BinancePublicClient(
        base_url="https://fapi.binance.com",
        timeout_seconds=1,
        retry_attempts=2,
        retry_base_delay_seconds=0.1,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )

    payload, _ = await client.exchange_time()

    assert payload["serverTime"] == 1_700_000_000_000
    assert attempts == 2
    assert delays == [0.1]


@pytest.mark.anyio
async def test_invalid_json_response_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", request=request)

    client = BinancePublicClient(
        base_url="https://fapi.binance.com",
        timeout_seconds=1,
        retry_attempts=1,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(BinancePublicClientError, match="invalid JSON"):
        await client.exchange_time()


@pytest.mark.anyio
async def test_endpoint_payload_types_are_validated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payloads: dict[str, object] = {
            "/fapi/v1/time": [],
            "/fapi/v1/exchangeInfo": [],
            "/fapi/v1/ticker/24hr": [],
            "/fapi/v1/klines": {},
        }
        return httpx.Response(200, json=payloads[request.url.path], request=request)

    client = BinancePublicClient(
        base_url="https://fapi.binance.com",
        timeout_seconds=1,
        retry_attempts=1,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(BinancePublicClientError, match="exchange-time"):
        await client.exchange_time()
    with pytest.raises(BinancePublicClientError, match="exchange-info"):
        await client.exchange_info()
    with pytest.raises(BinancePublicClientError, match="ticker"):
        await client.ticker_24h("BTCUSDT")
    with pytest.raises(BinancePublicClientError, match="kline"):
        await client.klines("BTCUSDT", "5m", 10)
