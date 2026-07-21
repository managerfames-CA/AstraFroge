"""Phase 2 shared snapshot, single-flight, versioning, and fail-closed tests."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.integrations.binance.public_client import BinancePublicClientError
from app.services.account_snapshot import AccountSnapshotService, SnapshotAwarePrivateClient
from app.services.shared_snapshots import (
    SharedClosedCandleMarketDataService,
    SharedIndicatorService,
)


class MarketFake:
    def __init__(self, last_closed_ms: int) -> None:
        self.last_closed_ms = last_closed_ms
        self.calls: list[tuple[str, str, int]] = []
        self.exchange_time_calls = 0
        self.fail = False

    async def exchange_time(self) -> tuple[dict[str, Any], int]:
        self.exchange_time_calls += 1
        return {"serverTime": self.last_closed_ms + 1}, 1

    async def klines(self, symbol: str, interval: str, limit: int) -> list[list[Any]]:
        self.calls.append((symbol, interval, limit))
        await asyncio.sleep(0.02)
        if self.fail:
            raise BinancePublicClientError("unavailable")
        interval_ms = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000}[interval]
        rows: list[list[Any]] = []
        for offset in range(limit - 1, -1, -1):
            close_ms = self.last_closed_ms - (offset * interval_ms)
            open_ms = close_ms - interval_ms + 1
            base = 100 + offset
            rows.append(
                [
                    open_ms,
                    str(base),
                    str(base + 2),
                    str(base - 2),
                    str(base + 1),
                    "10",
                    close_ms,
                    "1000",
                    5,
                ]
            )
        return rows


def _close_ms(value: datetime, interval_ms: int) -> int:
    now_ms = int(value.timestamp() * 1000)
    return now_ms - (now_ms % interval_ms) - 1


@pytest.mark.anyio
async def test_same_closed_candle_snapshot_is_fetched_once_and_reused() -> None:
    now = [datetime(2026, 7, 18, 12, 2, tzinfo=UTC)]
    fake = MarketFake(_close_ms(now[0], 3_600_000))
    service = SharedClosedCandleMarketDataService(
        fake,  # type: ignore[arg-type]
        stale_ttl_seconds=30,
        now_provider=lambda: now[0],
    )

    first = await service.candles("BTCUSDT", "1h", 200)
    second = await service.candles("BTCUSDT", "1h", 200)

    assert len(fake.calls) == 1
    assert fake.exchange_time_calls == 1
    assert first.snapshot_version == second.snapshot_version
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert service.cache_metrics().underlying_work_count == 1


@pytest.mark.anyio
async def test_concurrent_same_candle_snapshot_collapses_to_one_fetch() -> None:
    now = [datetime(2026, 7, 18, 12, 2, tzinfo=UTC)]
    fake = MarketFake(_close_ms(now[0], 900_000))
    service = SharedClosedCandleMarketDataService(
        fake,  # type: ignore[arg-type]
        now_provider=lambda: now[0],
    )

    results = await asyncio.gather(
        *(service.candles("BTCUSDT", "15m", 200) for _ in range(5))
    )

    assert len(fake.calls) == 1
    assert fake.exchange_time_calls == 1
    assert len({item.snapshot_version for item in results}) == 1


@pytest.mark.anyio
async def test_new_closed_candle_creates_new_version_and_refreshes_once() -> None:
    now = [datetime(2026, 7, 18, 12, 2, tzinfo=UTC)]
    fake = MarketFake(_close_ms(now[0], 3_600_000))
    service = SharedClosedCandleMarketDataService(
        fake,  # type: ignore[arg-type]
        now_provider=lambda: now[0],
        exchange_time_ttl_seconds=0,
    )
    first = await service.candles("BTCUSDT", "1h", 200)

    now[0] = now[0] + timedelta(hours=1)
    fake.last_closed_ms = _close_ms(now[0], 3_600_000)
    second = await service.candles("BTCUSDT", "1h", 200)
    third = await service.candles("BTCUSDT", "1h", 200)

    assert len(fake.calls) == 2
    assert first.snapshot_version != second.snapshot_version
    assert second.snapshot_version == third.snapshot_version


@pytest.mark.anyio
async def test_symbols_and_timeframes_do_not_share_candle_cache() -> None:
    now = [datetime(2026, 7, 18, 12, 2, tzinfo=UTC)]
    fake = MarketFake(_close_ms(now[0], 300_000))
    service = SharedClosedCandleMarketDataService(
        fake,  # type: ignore[arg-type]
        now_provider=lambda: now[0],
    )

    await service.candles("BTCUSDT", "5m", 50)
    await service.candles("ETHUSDT", "5m", 50)
    fake.last_closed_ms = _close_ms(now[0], 900_000)
    await service.candles("BTCUSDT", "15m", 50)

    assert len(fake.calls) == 3


@pytest.mark.anyio
async def test_expired_stale_market_snapshot_does_not_hide_refresh_failure() -> None:
    now = [datetime(2026, 7, 18, 12, 2, tzinfo=UTC)]
    fake = MarketFake(_close_ms(now[0], 3_600_000))
    service = SharedClosedCandleMarketDataService(
        fake,  # type: ignore[arg-type]
        stale_ttl_seconds=30,
        now_provider=lambda: now[0],
        exchange_time_ttl_seconds=0,
    )
    await service.candles("BTCUSDT", "1h", 50)

    now[0] = now[0] + timedelta(hours=1)
    fake.last_closed_ms = _close_ms(now[0], 3_600_000)
    fake.fail = True
    with pytest.raises(BinancePublicClientError):
        await service.candles("BTCUSDT", "1h", 50)


@pytest.mark.anyio
async def test_indicator_result_calculated_once_per_exact_candle_version() -> None:
    now = [datetime(2026, 7, 18, 12, 2, tzinfo=UTC)]
    fake = MarketFake(_close_ms(now[0], 3_600_000))
    market = SharedClosedCandleMarketDataService(
        fake,  # type: ignore[arg-type]
        now_provider=lambda: now[0],
        exchange_time_ttl_seconds=0,
    )
    indicators = SharedIndicatorService(market)

    first = await indicators.build("BTCUSDT", "1h", 220)
    second = await indicators.build("BTCUSDT", "1h", 220)
    assert indicators.cache_metrics().underlying_work_count == 1
    assert first.snapshot_version == second.snapshot_version
    assert second.cache_hit is True

    now[0] = now[0] + timedelta(hours=1)
    fake.last_closed_ms = _close_ms(now[0], 3_600_000)
    third = await indicators.build("BTCUSDT", "1h", 220)
    fourth = await indicators.build("BTCUSDT", "1h", 220)
    assert indicators.cache_metrics().underlying_work_count == 2
    assert third.snapshot_version != first.snapshot_version
    assert third.snapshot_version == fourth.snapshot_version


class PrivateFake:
    def __init__(self) -> None:
        self.account_calls = 0
        self.position_calls = 0
        self.income_calls = 0
        self.fail = False
        self.wallet = "1000"

    def account(self) -> dict[str, Any]:
        self.account_calls += 1
        time.sleep(0.02)
        if self.fail:
            raise BinanceDemoPrivateClientError("unavailable")
        return {
            "canTrade": True,
            "totalWalletBalance": self.wallet,
            "availableBalance": "800",
            "totalUnrealizedProfit": "5",
            "totalInitialMargin": "100",
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": self.wallet,
                    "availableBalance": "800",
                    "unrealizedProfit": "5",
                }
            ],
        }

    def positions(self) -> list[dict[str, Any]]:
        self.position_calls += 1
        return [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.01",
                "leverage": "3",
                "entryPrice": "65000",
                "unRealizedProfit": "5",
            }
        ]

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self.income_calls += 1
        return [{"incomeType": "REALIZED_PNL", "income": "2", "time": end_time_ms}]


def test_account_snapshot_shared_across_consumer_methods() -> None:
    fake = PrivateFake()
    service = AccountSnapshotService(fake, freshness_seconds=60)
    proxy = SnapshotAwarePrivateClient(fake, service)

    proxy.account()
    proxy.positions()
    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    proxy.income_history(
        start_time_ms=int(day_start.timestamp() * 1000),
        end_time_ms=int(now.timestamp() * 1000),
    )

    assert fake.account_calls == 1
    assert fake.position_calls == 1
    assert fake.income_calls == 1
    assert service.status().refresh_count == 1


def test_concurrent_account_refresh_is_single_flight() -> None:
    fake = PrivateFake()
    service = AccountSnapshotService(fake, freshness_seconds=60)

    with ThreadPoolExecutor(max_workers=5) as pool:
        snapshots = list(pool.map(lambda _: service.get(), range(5)))

    assert fake.account_calls == 1
    assert fake.position_calls == 1
    assert fake.income_calls == 1
    assert len({item.snapshot_id for item in snapshots}) == 1


def test_fresh_required_account_snapshot_does_not_reuse_cached_state() -> None:
    fake = PrivateFake()
    service = AccountSnapshotService(fake, freshness_seconds=60)
    first = service.get()
    fake.wallet = "1200"

    second = service.force_refresh()

    assert fake.account_calls == 2
    assert first.snapshot_id != second.snapshot_id
    assert second.total_wallet_balance_usdt > first.total_wallet_balance_usdt


def test_private_failure_when_fresh_state_required_fails_closed() -> None:
    fake = PrivateFake()
    service = AccountSnapshotService(fake, freshness_seconds=60)
    cached = service.get()
    fake.fail = True

    with pytest.raises(BinanceDemoPrivateClientError):
        service.force_refresh()

    status = service.status()
    assert cached.snapshot_id == status.snapshot_id
    assert status.refresh_error == "BinanceDemoPrivateClientError"
    assert fake.account_calls == 2
