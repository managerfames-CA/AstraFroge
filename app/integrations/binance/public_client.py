"""Async Binance USD-M Futures public REST client."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

import httpx

type SleepFunction = Callable[[float], Awaitable[None]]


class BinancePublicClientError(RuntimeError):
    """Stable adapter error that never exposes raw response bodies."""


class BinancePublicClient:
    """Bounded client for unauthenticated Binance USD-M Futures data."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        retry_attempts: int = 3,
        retry_base_delay_seconds: float = 0.25,
        rate_limit_max_delay_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: SleepFunction = asyncio.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._retry_attempts = retry_attempts
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._rate_limit_max_delay_seconds = rate_limit_max_delay_seconds
        self._transport = transport
        self._sleep = sleep

    @staticmethod
    def _retryable_server_status(status_code: int) -> bool:
        return 500 <= status_code <= 599

    def _retry_after_seconds(self, response: httpx.Response) -> float | None:
        raw_value = response.headers.get("Retry-After")
        if raw_value is None:
            return None
        try:
            delay = float(raw_value)
        except ValueError:
            return None
        if delay < 0 or delay > self._rate_limit_max_delay_seconds:
            return None
        return delay

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, int]:
        started = perf_counter()
        last_error: Exception | None = None
        rate_limited = False

        for attempt in range(self._retry_attempts):
            try:
                async with httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=self._timeout,
                    transport=self._transport,
                ) as client:
                    response = await client.get(path, params=params)
                    response.raise_for_status()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                rate_limited = False
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if status_code == 429:
                    rate_limited = True
                    last_error = exc
                    if attempt + 1 >= self._retry_attempts:
                        break
                    delay = self._retry_after_seconds(exc.response)
                    if delay is None:
                        raise BinancePublicClientError(
                            "Binance public market request was rate limited"
                        ) from exc
                    await self._sleep(delay)
                    continue
                if not self._retryable_server_status(status_code):
                    raise BinancePublicClientError(
                        f"Binance public market request failed with status {status_code}"
                    ) from exc
                last_error = exc
                rate_limited = False
            else:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise BinancePublicClientError(
                        "Binance returned an invalid JSON response"
                    ) from exc
                latency_ms = max(0, round((perf_counter() - started) * 1000))
                return payload, latency_ms

            if attempt + 1 < self._retry_attempts:
                delay = self._retry_base_delay_seconds * (2**attempt)
                await self._sleep(delay)

        message = (
            "Binance public market request was rate limited"
            if rate_limited
            else "Binance public market data is unavailable"
        )
        raise BinancePublicClientError(message) from last_error

    async def exchange_time(self) -> tuple[dict[str, Any], int]:
        payload, latency = await self._get("/fapi/v1/time")
        if not isinstance(payload, dict):
            raise BinancePublicClientError("Unexpected exchange-time response")
        return payload, latency

    async def exchange_info(self) -> dict[str, Any]:
        payload, _ = await self._get("/fapi/v1/exchangeInfo")
        if not isinstance(payload, dict):
            raise BinancePublicClientError("Unexpected exchange-info response")
        return payload

    async def ticker_24h(self, symbol: str) -> dict[str, Any]:
        payload, _ = await self._get("/fapi/v1/ticker/24hr", {"symbol": symbol})
        if not isinstance(payload, dict):
            raise BinancePublicClientError("Unexpected ticker response")
        return payload

    async def ticker_24h_all(self) -> list[dict[str, Any]]:
        payload, _ = await self._get("/fapi/v1/ticker/24hr")
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise BinancePublicClientError("Unexpected bulk ticker response")
        return payload

    async def book_tickers(self) -> list[dict[str, Any]]:
        payload, _ = await self._get("/fapi/v1/ticker/bookTicker")
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise BinancePublicClientError("Unexpected book-ticker response")
        return payload

    async def klines(self, symbol: str, interval: str, limit: int) -> list[list[Any]]:
        payload, _ = await self._get(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        if not isinstance(payload, list):
            raise BinancePublicClientError("Unexpected kline response")
        return payload
