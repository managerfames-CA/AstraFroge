"""Connection-pooled Binance clients preserving existing retry/signing behavior."""

from __future__ import annotations

from time import perf_counter
from typing import Any

import httpx

from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.integrations.binance.public_client import BinancePublicClient, BinancePublicClientError
from app.integrations.binance.recovery_demo_client import BinanceDemoRecoveryClient


class PooledBinancePublicClient(BinancePublicClient):
    """Reuse one AsyncClient/connection pool for process-scoped public requests."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._http_client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
        )

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, int]:
        started = perf_counter()
        last_error: Exception | None = None
        rate_limited = False
        for attempt in range(self._retry_attempts):
            try:
                response = await self._http_client.get(path, params=params)
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

    async def aclose(self) -> None:
        await self._http_client.aclose()


class PooledBinanceDemoRecoveryClient(BinanceDemoRecoveryClient):
    """Reuse one sync Client/connection pool for signed Demo and recovery requests."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._http_client = httpx.Client(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={"X-MBX-APIKEY": self._api_key},
            transport=self._transport,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = True,
    ) -> Any:
        request_params = self._signed_params(params) if signed else dict(params or {})
        try:
            response = self._http_client.request(method, path, params=request_params)
            response.raise_for_status()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise BinanceDemoPrivateClientError(
                "Binance demo private API is unavailable"
            ) from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            exchange_code: int | None = None
            try:
                payload = exc.response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("code"), int):
                exchange_code = payload["code"]
            message = f"Binance demo private API request failed with status {status_code}"
            if exchange_code is not None:
                message += f" and exchange code {exchange_code}"
            raise BinanceDemoPrivateClientError(
                message,
                status_code=status_code,
                exchange_code=exchange_code,
            ) from exc
        try:
            return response.json()
        except ValueError as exc:
            raise BinanceDemoPrivateClientError(
                "Binance demo private API returned invalid JSON"
            ) from exc

    def close(self) -> None:
        self._http_client.close()
