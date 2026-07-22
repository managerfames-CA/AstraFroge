"""Safety tests for Binance exchange-time-authoritative closed-candle snapshots."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from app.integrations.binance.public_client import BinancePublicClientError
from app.services.shared_snapshots import (
    SharedClosedCandleMarketDataService,
    SharedIndicatorService,
)

_INTERVAL_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000}


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _last_closed_ms(exchange_time_ms: int, interval: str) -> int:
    interval_ms = _INTERVAL_MS[interval]
    return exchange_time_ms - (exchange_time_ms % interval_ms) - 1


def _row(close_ms: int, interval: str, price: int) -> list[Any]:
    interval_ms = _INTERVAL_MS[interval]
    return [
        close_ms - interval_ms + 1,
        str(price),
        str(price + 2),
        str(price - 2),
        str(price + 1),
        "10",
        close_ms,
        "1000",
        5,
    ]


class ExchangeTimeMarketFake:
    def __init__(self, *, server_time_ms: int, rows: list[list[Any]]) -> None:
        self.server_time_ms = server_time_ms
        self.rows = rows
        self.exchange_time_calls = 0
        self.kline_calls = 0
        self.fail_exchange_time = False

    async def exchange_time(self) -> tuple[dict[str, Any], int]:
        self.exchange_time_calls += 1
        await asyncio.sleep(0.01)
        if self.fail_exchange_time:
            raise BinancePublicClientError("exchange time unavailable")
        return {"serverTime": self.server_time_ms}, 1

    async def klines(self, symbol: str, interval: str, limit: int) -> list[list[Any]]:
        self.kline_calls += 1
        await asyncio.sleep(0.01)
        return list(self.rows)


@pytest.mark.anyio
async def test_local_clock_ahead_never_promotes_exchange_open_candle() -> None:
    exchange_now = datetime(2026, 7, 18, 12, 2, tzinfo=UTC)
    server_time_ms = _ms(exchange_now)
    closed_ms = _last_closed_ms(server_time_ms, "5m")
    open_ms = closed_ms + _INTERVAL_MS["5m"]
    fake = ExchangeTimeMarketFake(
        server_time_ms=server_time_ms,
        rows=[_row(closed_ms, "5m", 100), _row(open_ms, "5m", 110)],
    )
    local_ahead = datetime(2026, 7, 18, 12, 20, tzinfo=UTC)
    service = SharedClosedCandleMarketDataService(
        fake,  # type: ignore[arg-type]
        now_provider=lambda: local_ahead,
    )

    snapshot = await service.candles("BTCUSDT", "5m", 2)

    assert [int(item.close_time.timestamp() * 1000) for item in snapshot.candles] == [closed_ms]
    assert snapshot.last_closed_candle_time == snapshot.candles[-1].close_time


@pytest.mark.anyio
async def test_local_clock_behind_still_recognizes_exchange_closed_candle() -> None:
    exchange_now = datetime(2026, 7, 18, 12, 7, tzinfo=UTC)
    server_time_ms = _ms(exchange_now)
    latest_closed_ms = _last_closed_ms(server_time_ms, "5m")
    previous_closed_ms = latest_closed_ms - _INTERVAL_MS["5m"]
    fake = ExchangeTimeMarketFake(
        server_time_ms=server_time_ms,
        rows=[
            _row(previous_closed_ms, "5m", 100),
            _row(latest_closed_ms, "5m", 110),
        ],
    )
    local_behind = datetime(2026, 7, 18, 11, 45, tzinfo=UTC)
    service = SharedClosedCandleMarketDataService(
        fake,  # type: ignore[arg-type]
        now_provider=lambda: local_behind,
    )

    snapshot = await service.candles("BTCUSDT", "5m", 2)

    assert int(snapshot.candles[-1].close_time.timestamp() * 1000) == latest_closed_ms
    assert snapshot.candle_count == 2


@pytest.mark.anyio
async def test_exact_exchange_interval_boundary_accepts_only_fully_closed_candle() -> None:
    exact_boundary = datetime(2026, 7, 18, 12, 5, tzinfo=UTC)
    server_time_ms = _ms(exact_boundary)
    fully_closed_ms = _last_closed_ms(server_time_ms, "5m")
    next_open_ms = fully_closed_ms + _INTERVAL_MS["5m"]
    fake = ExchangeTimeMarketFake(
        server_time_ms=server_time_ms,
        rows=[_row(fully_closed_ms, "5m", 100), _row(next_open_ms, "5m", 110)],
    )
    service = SharedClosedCandleMarketDataService(fake)  # type: ignore[arg-type]

    snapshot = await service.candles("BTCUSDT", "5m", 2)

    assert [int(item.close_time.timestamp() * 1000) for item in snapshot.candles] == [
        fully_closed_ms
    ]


@pytest.mark.anyio
async def test_concurrent_requests_bound_exchange_time_and_kline_fetches() -> None:
    server_time_ms = _ms(datetime(2026, 7, 18, 12, 7, tzinfo=UTC))
    closed_ms = _last_closed_ms(server_time_ms, "5m")
    fake = ExchangeTimeMarketFake(
        server_time_ms=server_time_ms,
        rows=[_row(closed_ms, "5m", 100)],
    )
    service = SharedClosedCandleMarketDataService(fake)  # type: ignore[arg-type]

    snapshots = await asyncio.gather(*(service.candles("BTCUSDT", "5m", 1) for _ in range(5)))

    assert fake.exchange_time_calls == 1
    assert fake.kline_calls == 1
    assert len({item.snapshot_version for item in snapshots}) == 1
    metrics = service.cache_metrics()
    assert metrics.exchange_time_lookups == 1
    assert metrics.underlying_work_count == 1


@pytest.mark.anyio
async def test_new_exchange_confirmed_candle_advances_snapshot_once() -> None:
    ticks = [0.0]
    server_time_ms = _ms(datetime(2026, 7, 18, 12, 2, tzinfo=UTC))
    first_closed_ms = _last_closed_ms(server_time_ms, "5m")
    fake = ExchangeTimeMarketFake(
        server_time_ms=server_time_ms,
        rows=[_row(first_closed_ms, "5m", 100)],
    )
    service = SharedClosedCandleMarketDataService(
        fake,  # type: ignore[arg-type]
        monotonic_provider=lambda: ticks[0],
        exchange_time_ttl_seconds=1,
    )
    first = await service.candles("BTCUSDT", "5m", 1)

    ticks[0] = 2.0
    fake.server_time_ms += _INTERVAL_MS["5m"]
    second_closed_ms = _last_closed_ms(fake.server_time_ms, "5m")
    fake.rows = [_row(second_closed_ms, "5m", 110)]
    refreshed = await asyncio.gather(*(service.candles("BTCUSDT", "5m", 1) for _ in range(3)))

    assert fake.exchange_time_calls == 2
    assert fake.kline_calls == 2
    assert first.snapshot_version != refreshed[0].snapshot_version
    assert len({item.snapshot_version for item in refreshed}) == 1


@pytest.mark.anyio
async def test_indicator_uses_only_exchange_confirmed_candle_version() -> None:
    server_time_ms = _ms(datetime(2026, 7, 18, 12, 7, tzinfo=UTC))
    latest_closed_ms = _last_closed_ms(server_time_ms, "5m")
    previous_closed_ms = latest_closed_ms - _INTERVAL_MS["5m"]
    open_ms = latest_closed_ms + _INTERVAL_MS["5m"]
    fake = ExchangeTimeMarketFake(
        server_time_ms=server_time_ms,
        rows=[
            _row(previous_closed_ms, "5m", 100),
            _row(latest_closed_ms, "5m", 110),
            _row(open_ms, "5m", 120),
        ],
    )
    market = SharedClosedCandleMarketDataService(fake)  # type: ignore[arg-type]
    indicators = SharedIndicatorService(market)

    candle_snapshot = await market.candles("BTCUSDT", "5m", 2)
    indicator_snapshot = await indicators.build("BTCUSDT", "5m", 2)

    assert indicator_snapshot.source_candle_version == candle_snapshot.snapshot_version
    assert indicator_snapshot.points[-1].close_time == candle_snapshot.candles[-1].close_time
    assert int(indicator_snapshot.points[-1].close_time.timestamp() * 1000) == latest_closed_ms


@pytest.mark.anyio
async def test_exchange_time_failure_never_promotes_new_version() -> None:
    ticks = [0.0]
    local_now = datetime.now(UTC)
    server_time_ms = _ms(local_now)
    first_closed_ms = _last_closed_ms(server_time_ms, "5m")
    fake = ExchangeTimeMarketFake(
        server_time_ms=server_time_ms,
        rows=[_row(first_closed_ms, "5m", 100)],
    )
    service = SharedClosedCandleMarketDataService(
        fake,  # type: ignore[arg-type]
        now_provider=lambda: local_now,
        monotonic_provider=lambda: ticks[0],
        exchange_time_ttl_seconds=1,
        stale_ttl_seconds=30,
    )
    first = await service.candles("BTCUSDT", "5m", 1)

    ticks[0] = 2.0
    fake.server_time_ms += _INTERVAL_MS["5m"]
    fake.rows = [_row(_last_closed_ms(fake.server_time_ms, "5m"), "5m", 110)]
    fake.fail_exchange_time = True
    stale = await service.candles("BTCUSDT", "5m", 1)

    assert stale.stale is True
    assert stale.snapshot_version == first.snapshot_version
    assert fake.kline_calls == 1

    no_cache = SharedClosedCandleMarketDataService(
        fake,  # type: ignore[arg-type]
        exchange_time_ttl_seconds=0,
    )
    with pytest.raises(BinancePublicClientError):
        await no_cache.candles("ETHUSDT", "5m", 1)
    assert fake.kline_calls == 1
