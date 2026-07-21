"""Public market-data normalization service."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.integrations.binance.public_client import BinancePublicClient, BinancePublicClientError
from app.schemas.market import (
    MarketCandle,
    MarketCandleSeries,
    MarketStatus,
    MarketSymbol,
    MarketTicker,
)

_ALLOWED_INTERVALS = {"5m", "15m", "1h"}


def _utc_from_ms(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid numeric market-data value") from exc


class MarketDataService:
    """Validate, normalize, cache, and safely degrade public market data."""

    def __init__(
        self,
        client: BinancePublicClient,
        cache_ttl_seconds: float = 2.0,
        stale_ttl_seconds: float = 30.0,
    ) -> None:
        self._client = client
        self._cache_ttl_seconds = cache_ttl_seconds
        self._stale_ttl_seconds = stale_ttl_seconds
        self._ticker_cache: dict[str, MarketTicker] = {}
        self._candle_cache: dict[tuple[str, str, int], MarketCandleSeries] = {}

    @staticmethod
    def _age_seconds(fetched_at: datetime) -> float:
        return max(0.0, (datetime.now(UTC) - fetched_at).total_seconds())

    def _fresh_ticker(self, symbol: str) -> MarketTicker | None:
        cached = self._ticker_cache.get(symbol)
        if cached is None:
            return None
        age = self._age_seconds(cached.fetched_at)
        if age > self._cache_ttl_seconds:
            return None
        return cached.model_copy(update={"stale": False, "cache_age_seconds": age})

    def _stale_ticker(self, symbol: str) -> MarketTicker | None:
        cached = self._ticker_cache.get(symbol)
        if cached is None:
            return None
        age = self._age_seconds(cached.fetched_at)
        if age > self._stale_ttl_seconds:
            return None
        return cached.model_copy(update={"stale": True, "cache_age_seconds": age})

    def _fresh_candles(self, key: tuple[str, str, int]) -> MarketCandleSeries | None:
        cached = self._candle_cache.get(key)
        if cached is None:
            return None
        age = self._age_seconds(cached.fetched_at)
        if age > self._cache_ttl_seconds:
            return None
        return cached.model_copy(update={"stale": False, "cache_age_seconds": age})

    def _stale_candles(self, key: tuple[str, str, int]) -> MarketCandleSeries | None:
        cached = self._candle_cache.get(key)
        if cached is None:
            return None
        age = self._age_seconds(cached.fetched_at)
        if age > self._stale_ttl_seconds:
            return None
        return cached.model_copy(update={"stale": True, "cache_age_seconds": age})

    async def status(self) -> MarketStatus:
        checked_at = datetime.now(UTC)
        payload, latency = await self._client.exchange_time()
        return MarketStatus(
            state="connected",
            checked_at=checked_at,
            exchange_time=_utc_from_ms(payload["serverTime"]),
            latency_ms=latency,
        )

    async def symbols(self) -> list[MarketSymbol]:
        payload = await self._client.exchange_info()
        raw_symbols = payload.get("symbols", [])
        if not isinstance(raw_symbols, list):
            raise ValueError("Invalid exchange-info symbols payload")
        result: list[MarketSymbol] = []
        for item in raw_symbols:
            if not isinstance(item, dict) or item.get("quoteAsset") != "USDT":
                continue
            if item.get("contractType") != "PERPETUAL":
                continue
            result.append(
                MarketSymbol(
                    symbol=str(item["symbol"]),
                    base_asset=str(item["baseAsset"]),
                    quote_asset=str(item["quoteAsset"]),
                    contract_type=str(item["contractType"]),
                    status=str(item["status"]),
                    price_precision=int(item["pricePrecision"]),
                    quantity_precision=int(item["quantityPrecision"]),
                )
            )
        return sorted(result, key=lambda item: item.symbol)

    async def ticker(self, symbol: str) -> MarketTicker:
        fresh = self._fresh_ticker(symbol)
        if fresh is not None:
            return fresh
        try:
            item = await self._client.ticker_24h(symbol)
        except BinancePublicClientError:
            stale = self._stale_ticker(symbol)
            if stale is not None:
                return stale
            raise
        fetched_at = datetime.now(UTC)
        ticker = MarketTicker(
            symbol=str(item["symbol"]),
            last_price=_decimal(item["lastPrice"]),
            price_change_percent=_decimal(item["priceChangePercent"]),
            high_price=_decimal(item["highPrice"]),
            low_price=_decimal(item["lowPrice"]),
            quote_volume=_decimal(item["quoteVolume"]),
            close_time=_utc_from_ms(item["closeTime"]),
            fetched_at=fetched_at,
        )
        self._ticker_cache[symbol] = ticker
        return ticker

    async def candles(self, symbol: str, interval: str, limit: int) -> MarketCandleSeries:
        if interval not in _ALLOWED_INTERVALS:
            raise ValueError("Interval must be one of: 5m, 15m, 1h")
        if limit < 1 or limit > 1000:
            raise ValueError("Kline limit must be between 1 and 1000")
        key = (symbol, interval, limit)
        fresh = self._fresh_candles(key)
        if fresh is not None:
            return fresh
        try:
            rows = await self._client.klines(symbol, interval, limit + 1)
        except BinancePublicClientError:
            stale = self._stale_candles(key)
            if stale is not None:
                return stale
            raise
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        candles: list[MarketCandle] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 9:
                raise ValueError("Invalid kline row")
            if int(row[6]) >= now_ms:
                continue
            candles.append(
                MarketCandle(
                    open_time=_utc_from_ms(row[0]),
                    close_time=_utc_from_ms(row[6]),
                    open=_decimal(row[1]),
                    high=_decimal(row[2]),
                    low=_decimal(row[3]),
                    close=_decimal(row[4]),
                    volume=_decimal(row[5]),
                    quote_volume=_decimal(row[7]),
                    trades=int(row[8]),
                )
            )
        series = MarketCandleSeries(
            symbol=symbol,
            interval=interval,
            fetched_at=datetime.now(UTC),
            candles=candles[-limit:],
        )
        self._candle_cache[key] = series
        return series
