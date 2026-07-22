"""Indicator Engine API integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.api.v1.dependencies import get_indicator_service
from app.core.config import Settings
from app.integrations.binance.public_client import BinancePublicClientError
from app.main import create_app
from app.schemas.indicators import IndicatorPoint, IndicatorSeries, MarketStructure


class FakeIndicatorService:
    def __init__(self, failure: Exception | None = None) -> None:
        self._failure = failure

    async def build(self, symbol: str, interval: str, limit: int) -> IndicatorSeries:
        if self._failure is not None:
            raise self._failure
        now = datetime.now(UTC)
        return IndicatorSeries(
            symbol=symbol,
            interval=interval,
            generated_at=now,
            candle_count=1,
            warmup_complete=False,
            stale=False,
            structure=MarketStructure(
                state="insufficient_data",
                lookback=20,
                support=Decimal("99"),
                resistance=Decimal("101"),
            ),
            points=[
                IndicatorPoint(
                    close_time=now,
                    close=Decimal("100"),
                    vwap=Decimal("100"),
                    volume=Decimal("10"),
                )
            ],
        )


async def _request(path: str, service: FakeIndicatorService) -> httpx.Response:
    application = create_app(Settings(environment="test"))
    application.dependency_overrides[get_indicator_service] = lambda: service
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.anyio
async def test_indicator_endpoint_returns_closed_candle_series() -> None:
    response = await _request(
        "/api/v1/indicators/btcusdt?interval=5m&limit=250",
        FakeIndicatorService(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "BTCUSDT"
    assert payload["interval"] == "5m"
    assert payload["points"][0]["close"] == "100"


@pytest.mark.anyio
async def test_indicator_endpoint_rejects_invalid_symbol() -> None:
    response = await _request(
        "/api/v1/indicators/BTC-USDT",
        FakeIndicatorService(),
    )

    assert response.status_code == 422


@pytest.mark.anyio
@pytest.mark.parametrize(
    "query",
    ["interval=4h", "limit=0", "limit=1001"],
)
async def test_indicator_query_validation(query: str) -> None:
    response = await _request(
        f"/api/v1/indicators/BTCUSDT?{query}",
        FakeIndicatorService(),
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_indicator_upstream_failure_maps_to_503() -> None:
    response = await _request(
        "/api/v1/indicators/BTCUSDT",
        FakeIndicatorService(BinancePublicClientError("market unavailable")),
    )

    assert response.status_code == 503


@pytest.mark.anyio
async def test_indicator_input_failure_maps_to_502() -> None:
    response = await _request(
        "/api/v1/indicators/BTCUSDT",
        FakeIndicatorService(ValueError("invalid candles")),
    )

    assert response.status_code == 502
