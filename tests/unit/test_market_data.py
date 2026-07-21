"""Market-data service unit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.integrations.binance.public_client import BinancePublicClientError
from app.services.market_data import MarketDataService


class FakeClient:
    def __init__(self) -> None:
        self.ticker_calls = 0
        self.kline_calls = 0
        self.fail_ticker = False
        self.fail_klines = False

    async def exchange_time(self) -> tuple[dict[str, Any], int]:
        return {"serverTime": 1_700_000_000_000}, 12

    async def exchange_info(self) -> dict[str, Any]:
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "pricePrecision": 2,
                    "quantityPrecision": 3,
                },
                {
                    "symbol": "BTCUSDC",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDC",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "pricePrecision": 2,
                    "quantityPrecision": 3,
                },
            ]
        }

    async def ticker_24h(self, symbol: str) -> dict[str, Any]:
        self.ticker_calls += 1
        if self.fail_ticker:
            raise BinancePublicClientError("unavailable")
        return {
            "symbol": symbol,
            "lastPrice": "65000.50",
            "priceChangePercent": "1.25",
            "highPrice": "66000",
            "lowPrice": "63000",
            "quoteVolume": "100000000",
            "closeTime": 1_700_000_000_000,
        }

    async def klines(self, symbol: str, interval: str, limit: int) -> list[list[Any]]:
        self.kline_calls += 1
        if self.fail_klines:
            raise BinancePublicClientError("unavailable")
        closed_at = int(datetime.now(UTC).timestamp() * 1000) - 60_000
        open_at = closed_at - 300_000
        return [
            [
                open_at,
                "1",
                "2",
                "0.5",
                "1.5",
                "10",
                closed_at,
                "15",
                5,
            ]
        ]


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def service(client: FakeClient) -> MarketDataService:
    return MarketDataService(client)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_status_reports_exchange_time_and_latency(service: MarketDataService) -> None:
    status = await service.status()
    assert status.state == "connected"
    assert status.latency_ms == 12
    assert status.exchange_time is not None


@pytest.mark.anyio
async def test_symbols_include_only_usdt_perpetuals(service: MarketDataService) -> None:
    symbols = await service.symbols()
    assert [item.symbol for item in symbols] == ["BTCUSDT"]


@pytest.mark.anyio
async def test_ticker_is_normalized(service: MarketDataService) -> None:
    ticker = await service.ticker("BTCUSDT")
    assert ticker.symbol == "BTCUSDT"
    assert str(ticker.last_price) == "65000.50"
    assert ticker.stale is False


@pytest.mark.anyio
async def test_candles_are_closed_and_normalized(service: MarketDataService) -> None:
    series = await service.candles("BTCUSDT", "5m", 10)
    assert len(series.candles) == 1
    assert series.candles[0].closed is True
    assert series.stale is False


@pytest.mark.anyio
async def test_invalid_interval_is_rejected(service: MarketDataService) -> None:
    with pytest.raises(ValueError, match="Interval must be one of"):
        await service.candles("BTCUSDT", "4h", 10)


@pytest.mark.anyio
async def test_fresh_ticker_cache_avoids_duplicate_exchange_call(
    client: FakeClient,
) -> None:
    cached_service = MarketDataService(
        client,  # type: ignore[arg-type]
        cache_ttl_seconds=60,
        stale_ttl_seconds=60,
    )

    first = await cached_service.ticker("BTCUSDT")
    second = await cached_service.ticker("BTCUSDT")

    assert first.symbol == second.symbol
    assert second.stale is False
    assert client.ticker_calls == 1


@pytest.mark.anyio
async def test_ticker_returns_bounded_stale_cache_on_exchange_failure(
    client: FakeClient,
) -> None:
    cached_service = MarketDataService(
        client,  # type: ignore[arg-type]
        cache_ttl_seconds=0,
        stale_ttl_seconds=60,
    )
    await cached_service.ticker("BTCUSDT")
    client.fail_ticker = True

    stale = await cached_service.ticker("BTCUSDT")

    assert stale.stale is True
    assert stale.cache_age_seconds >= 0
    assert client.ticker_calls == 2


@pytest.mark.anyio
async def test_candles_return_bounded_stale_cache_on_exchange_failure(
    client: FakeClient,
) -> None:
    cached_service = MarketDataService(
        client,  # type: ignore[arg-type]
        cache_ttl_seconds=0,
        stale_ttl_seconds=60,
    )
    await cached_service.candles("BTCUSDT", "5m", 10)
    client.fail_klines = True

    stale = await cached_service.candles("BTCUSDT", "5m", 10)

    assert stale.stale is True
    assert len(stale.candles) == 1
    assert client.kline_calls == 2
