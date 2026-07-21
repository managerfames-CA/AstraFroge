"""Malformed market-data payload tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.market_data import MarketDataService


class MalformedClient:
    async def exchange_time(self) -> tuple[dict[str, Any], int]:
        return {"serverTime": "invalid"}, 1

    async def exchange_info(self) -> dict[str, Any]:
        return {"symbols": "invalid"}

    async def ticker_24h(self, symbol: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "lastPrice": "not-a-number",
            "priceChangePercent": "1",
            "highPrice": "2",
            "lowPrice": "0.5",
            "quoteVolume": "10",
            "closeTime": 1_700_000_000_000,
        }

    async def klines(self, symbol: str, interval: str, limit: int) -> list[list[Any]]:
        return [[1, "1"]]


@pytest.fixture
def service() -> MarketDataService:
    return MarketDataService(MalformedClient())  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_invalid_exchange_info_symbols_payload_is_rejected(
    service: MarketDataService,
) -> None:
    with pytest.raises(ValueError, match="Invalid exchange-info symbols payload"):
        await service.symbols()


@pytest.mark.anyio
async def test_invalid_ticker_numeric_payload_is_rejected(service: MarketDataService) -> None:
    with pytest.raises(ValueError, match="Invalid numeric market-data value"):
        await service.ticker("BTCUSDT")


@pytest.mark.anyio
async def test_invalid_kline_row_is_rejected(service: MarketDataService) -> None:
    with pytest.raises(ValueError, match="Invalid kline row"):
        await service.candles("BTCUSDT", "5m", 10)
