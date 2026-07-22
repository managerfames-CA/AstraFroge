"""Market API integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.api.v1.dependencies import get_market_service
from app.core.config import Settings
from app.integrations.binance.public_client import BinancePublicClientError
from app.main import create_app
from app.schemas.market import MarketTicker


class FakeMarketService:
    def __init__(self, *, fail_ticker: bool = False) -> None:
        self.fail_ticker = fail_ticker

    async def ticker(self, symbol: str) -> MarketTicker:
        if self.fail_ticker:
            raise BinancePublicClientError("Binance public market data is unavailable")
        now = datetime.now(UTC)
        return MarketTicker(
            symbol=symbol,
            last_price=Decimal("65000.50"),
            price_change_percent=Decimal("1.25"),
            high_price=Decimal("66000"),
            low_price=Decimal("63000"),
            quote_volume=Decimal("100000000"),
            close_time=now,
            fetched_at=now,
            stale=True,
            cache_age_seconds=3.5,
        )

    async def candles(self, symbol: str, interval: str, limit: int):  # type: ignore[no-untyped-def]
        raise ValueError("Interval must be one of: 5m, 15m, 1h")


async def _request(
    path: str,
    service: FakeMarketService,
) -> httpx.Response:
    application = create_app(Settings(environment="test"))
    application.dependency_overrides[get_market_service] = lambda: service
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.anyio
async def test_ticker_endpoint_returns_stale_metadata() -> None:
    response = await _request("/api/v1/market/ticker/btcusdt", FakeMarketService())

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "BTCUSDT"
    assert payload["stale"] is True
    assert payload["cache_age_seconds"] == 3.5


@pytest.mark.anyio
async def test_invalid_symbol_is_rejected_before_service_call() -> None:
    response = await _request("/api/v1/market/ticker/BTC-USDT", FakeMarketService())

    assert response.status_code == 422


@pytest.mark.anyio
async def test_invalid_kline_limit_is_rejected_by_query_validation() -> None:
    response = await _request(
        "/api/v1/market/klines/BTCUSDT?interval=5m&limit=1001",
        FakeMarketService(),
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_upstream_failure_maps_to_service_unavailable() -> None:
    response = await _request(
        "/api/v1/market/ticker/BTCUSDT",
        FakeMarketService(fail_ticker=True),
    )

    assert response.status_code == 503
