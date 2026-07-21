"""Phase 2 shared closed-candle and indicator snapshot caches."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from app.integrations.binance.public_client import BinancePublicClientError
from app.schemas.indicators import IndicatorSeries
from app.schemas.market import MarketCandle, MarketCandleSeries
from app.services.indicators import IndicatorService
from app.services.market_data import MarketDataService, _decimal, _utc_from_ms

_INTERVAL_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000}
INDICATOR_ENGINE_VERSION = "custom-deterministic-v1"


@dataclass(frozen=True)
class SnapshotCacheMetrics:
    """Small non-secret cache instrumentation surface."""

    cache_hits: int
    cache_misses: int
    underlying_work_count: int
    last_success_at: datetime | None
    last_error: str | None
    exchange_time_lookups: int = 0
    exchange_time_cache_hits: int = 0


def _candle_data_version(candles: list[MarketCandle]) -> str:
    digest = hashlib.sha256()
    for candle in candles:
        digest.update(
            "|".join(
                (
                    candle.open_time.isoformat(),
                    candle.close_time.isoformat(),
                    format(candle.open, "f"),
                    format(candle.high, "f"),
                    format(candle.low, "f"),
                    format(candle.close, "f"),
                    format(candle.volume, "f"),
                    format(candle.quote_volume, "f"),
                    str(candle.trades),
                )
            ).encode()
        )
    return digest.hexdigest()


def _candle_snapshot_version(
    symbol: str,
    interval: str,
    last_closed: datetime | None,
    data_version: str,
) -> str:
    last_closed_identity = last_closed.isoformat() if last_closed else "none"
    identity = f"{symbol}|{interval}|{last_closed_identity}|{data_version}"
    return hashlib.sha256(identity.encode()).hexdigest()


class SharedClosedCandleMarketDataService(MarketDataService):
    """Reuse exchange-time-verified closed-candle snapshots across consumers."""

    def __init__(
        self,
        *args: Any,
        now_provider: Callable[[], datetime] | None = None,
        monotonic_provider: Callable[[], float] | None = None,
        exchange_time_ttl_seconds: float = 1.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if exchange_time_ttl_seconds < 0:
            raise ValueError("Exchange-time cache TTL must be non-negative")
        self._snapshot_now = now_provider or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_provider or monotonic
        self._exchange_time_ttl_seconds = exchange_time_ttl_seconds
        self._shared_snapshots: dict[tuple[str, str], MarketCandleSeries] = {}
        self._resource_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._exchange_time_lock = asyncio.Lock()
        self._exchange_server_time_ms: int | None = None
        self._exchange_time_cached_at_tick: float | None = None
        self._snapshot_hits = 0
        self._snapshot_misses = 0
        self._underlying_fetches = 0
        self._exchange_time_lookups = 0
        self._exchange_time_cache_hits = 0
        self._last_snapshot_fetch: datetime | None = None
        self._last_snapshot_error: str | None = None

    def cache_metrics(self) -> SnapshotCacheMetrics:
        return SnapshotCacheMetrics(
            cache_hits=self._snapshot_hits,
            cache_misses=self._snapshot_misses,
            underlying_work_count=self._underlying_fetches,
            last_success_at=self._last_snapshot_fetch,
            last_error=self._last_snapshot_error,
            exchange_time_lookups=self._exchange_time_lookups,
            exchange_time_cache_hits=self._exchange_time_cache_hits,
        )

    def _cached_exchange_time_ms(self, tick: float) -> int | None:
        cached_at = self._exchange_time_cached_at_tick
        server_time_ms = self._exchange_server_time_ms
        if cached_at is None or server_time_ms is None:
            return None
        age = max(0.0, tick - cached_at)
        if age > self._exchange_time_ttl_seconds:
            return None
        self._exchange_time_cache_hits += 1
        return server_time_ms

    async def _exchange_time_ms(self) -> int:
        tick = self._monotonic()
        cached = self._cached_exchange_time_ms(tick)
        if cached is not None:
            return cached

        async with self._exchange_time_lock:
            tick = self._monotonic()
            cached = self._cached_exchange_time_ms(tick)
            if cached is not None:
                return cached

            self._exchange_time_lookups += 1
            payload, _ = await self._client.exchange_time()
            raw_server_time = payload.get("serverTime")
            if raw_server_time is None or isinstance(raw_server_time, bool):
                raise ValueError("Invalid Binance exchange-time payload")
            try:
                server_time_ms = int(raw_server_time)
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid Binance exchange-time payload") from exc
            if server_time_ms < 0:
                raise ValueError("Invalid Binance exchange-time payload")
            self._exchange_server_time_ms = server_time_ms
            self._exchange_time_cached_at_tick = tick
            return server_time_ms

    @staticmethod
    def _expected_last_closed(interval: str, exchange_time_ms: int) -> datetime:
        interval_ms = _INTERVAL_MS[interval]
        boundary_ms = exchange_time_ms - (exchange_time_ms % interval_ms)
        return _utc_from_ms(boundary_ms - 1)

    @staticmethod
    def _snapshot_reusable(
        snapshot: MarketCandleSeries,
        *,
        limit: int,
        expected_last_closed: datetime,
    ) -> bool:
        return bool(
            len(snapshot.candles) >= limit
            and snapshot.last_closed_candle_time is not None
            and snapshot.last_closed_candle_time == expected_last_closed
        )

    @staticmethod
    def _slice_snapshot(
        snapshot: MarketCandleSeries,
        limit: int,
        *,
        cache_hit: bool,
    ) -> MarketCandleSeries:
        candles = snapshot.candles[-limit:]
        data_version = _candle_data_version(candles)
        last_closed = candles[-1].close_time if candles else None
        return snapshot.model_copy(
            update={
                "candles": candles,
                "candle_count": len(candles),
                "last_closed_candle_time": last_closed,
                "data_version": data_version,
                "snapshot_version": _candle_snapshot_version(
                    snapshot.symbol,
                    snapshot.interval,
                    last_closed,
                    data_version,
                ),
                "cache_hit": cache_hit,
                "cache_age_seconds": self_age_seconds(snapshot.fetched_at),
            }
        )

    def _stale_verified_snapshot(
        self,
        resource: tuple[str, str],
        *,
        limit: int,
    ) -> MarketCandleSeries | None:
        cached = self._shared_snapshots.get(resource)
        if (
            cached is None
            or len(cached.candles) < limit
            or self_age_seconds(cached.fetched_at) > self._stale_ttl_seconds
        ):
            return None
        stale = cached.model_copy(update={"stale": True})
        return self._slice_snapshot(stale, limit, cache_hit=True)

    async def candles(self, symbol: str, interval: str, limit: int) -> MarketCandleSeries:
        if interval not in _INTERVAL_MS:
            raise ValueError("Interval must be one of: 5m, 15m, 1h")
        if limit < 1 or limit > 1000:
            raise ValueError("Kline limit must be between 1 and 1000")

        resource = (symbol, interval)
        try:
            exchange_time_ms = await self._exchange_time_ms()
        except (BinancePublicClientError, ValueError) as exc:
            self._last_snapshot_error = type(exc).__name__
            stale = self._stale_verified_snapshot(resource, limit=limit)
            if stale is not None:
                return stale
            raise

        expected_last_closed = self._expected_last_closed(interval, exchange_time_ms)
        cached = self._shared_snapshots.get(resource)
        if cached is not None and self._snapshot_reusable(
            cached,
            limit=limit,
            expected_last_closed=expected_last_closed,
        ):
            self._snapshot_hits += 1
            return self._slice_snapshot(cached, limit, cache_hit=True)

        self._snapshot_misses += 1
        lock = self._resource_locks.setdefault(resource, asyncio.Lock())
        async with lock:
            cached = self._shared_snapshots.get(resource)
            if cached is not None and self._snapshot_reusable(
                cached,
                limit=limit,
                expected_last_closed=expected_last_closed,
            ):
                self._snapshot_hits += 1
                return self._slice_snapshot(cached, limit, cache_hit=True)

            try:
                self._underlying_fetches += 1
                rows = await self._client.klines(symbol, interval, limit + 1)
            except BinancePublicClientError as exc:
                self._last_snapshot_error = type(exc).__name__
                stale = self._stale_verified_snapshot(resource, limit=limit)
                if stale is not None:
                    return stale
                raise

            expected_last_closed_ms = int(expected_last_closed.timestamp() * 1000)
            candles: list[MarketCandle] = []
            for row in rows:
                if not isinstance(row, list) or len(row) < 9:
                    raise ValueError("Invalid kline row")
                if int(row[6]) > expected_last_closed_ms:
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
            candles = candles[-limit:]
            data_version = _candle_data_version(candles)
            last_closed = candles[-1].close_time if candles else None
            snapshot = MarketCandleSeries(
                symbol=symbol,
                interval=interval,
                fetched_at=self._snapshot_now(),
                stale=False,
                last_closed_candle_time=last_closed,
                candle_count=len(candles),
                data_version=data_version,
                snapshot_version=_candle_snapshot_version(
                    symbol, interval, last_closed, data_version
                ),
                cache_hit=False,
                candles=candles,
            )
            self._shared_snapshots[resource] = snapshot
            self._last_snapshot_fetch = snapshot.fetched_at
            self._last_snapshot_error = None
            return snapshot


def self_age_seconds(fetched_at: datetime) -> float:
    """Return non-negative UTC age without coupling cache validity to arbitrary TTL."""

    return max(0.0, (datetime.now(UTC) - fetched_at).total_seconds())


class SharedIndicatorService(IndicatorService):
    """Calculate the custom deterministic indicators once per exact candle version."""

    def __init__(self, candle_provider: Any) -> None:
        super().__init__(candle_provider)
        self._indicator_cache: dict[tuple[str, str], IndicatorSeries] = {}
        self._indicator_locks: dict[tuple[str, str, int], asyncio.Lock] = {}
        self._indicator_hits = 0
        self._indicator_misses = 0
        self._indicator_calculations = 0
        self._last_calculation: datetime | None = None
        self._last_indicator_error: str | None = None

    def cache_metrics(self) -> SnapshotCacheMetrics:
        return SnapshotCacheMetrics(
            cache_hits=self._indicator_hits,
            cache_misses=self._indicator_misses,
            underlying_work_count=self._indicator_calculations,
            last_success_at=self._last_calculation,
            last_error=self._last_indicator_error,
        )

    async def build(self, symbol: str, interval: str, limit: int) -> IndicatorSeries:
        series = await self._candle_provider.candles(symbol, interval, limit)
        source_version = series.snapshot_version or _candle_snapshot_version(
            series.symbol,
            series.interval,
            series.candles[-1].close_time if series.candles else None,
            _candle_data_version(series.candles),
        )
        cache_key = (source_version, INDICATOR_ENGINE_VERSION)
        cached = self._indicator_cache.get(cache_key)
        if cached is not None:
            self._indicator_hits += 1
            return cached.model_copy(
                update={
                    "cache_hit": True,
                    "cache_age_seconds": self_age_seconds(cached.generated_at),
                    "stale": series.stale,
                }
            )

        self._indicator_misses += 1
        resource = (symbol, interval, limit)
        lock = self._indicator_locks.setdefault(resource, asyncio.Lock())
        async with lock:
            cached = self._indicator_cache.get(cache_key)
            if cached is not None:
                self._indicator_hits += 1
                return cached.model_copy(
                    update={
                        "cache_hit": True,
                        "cache_age_seconds": self_age_seconds(cached.generated_at),
                        "stale": series.stale,
                    }
                )
            try:
                self._indicator_calculations += 1
                calculated = self._engine.calculate(series)
            except Exception as exc:
                self._last_indicator_error = type(exc).__name__
                raise
            snapshot_version = hashlib.sha256(
                f"{source_version}|{INDICATOR_ENGINE_VERSION}".encode()
            ).hexdigest()
            calculated = calculated.model_copy(
                update={
                    "source_candle_version": source_version,
                    "indicator_engine_version": INDICATOR_ENGINE_VERSION,
                    "snapshot_version": snapshot_version,
                    "cache_hit": False,
                }
            )
            self._indicator_cache[cache_key] = calculated
            self._last_calculation = calculated.generated_at
            self._last_indicator_error = None
            return calculated
