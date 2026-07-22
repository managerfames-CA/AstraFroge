"""Shared deterministic Scanner test builders and fakes."""

from __future__ import annotations

# ruff: noqa: F401
import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MethodType
from typing import Any

import pytest

from app.schemas.indicators import IndicatorPoint, IndicatorSeries, MarketStructure
from app.schemas.market import MarketCandle, MarketCandleSeries, MarketStatus
from app.schemas.scanner import (
    CandidateLifecycle,
    ScannerAuditRecord,
    ScannerCandidate,
    ScannerDirection,
    ScannerGrade,
    ScannerRunStatus,
    ScannerSetup,
    ScannerState,
)
from app.schemas.universe import UniverseCandidate, UniverseSnapshot
from app.services.scanner import ScannerService
from app.services.scanner_base import (
    EvaluationContext,
    Frame,
    ScannerEvaluationError,
    _candidate_key,
    _directional_break_margin,
    _directional_close_position,
    _directional_delta,
    _directional_extreme,
    _directional_histogram,
    _directional_rsi_margin,
    _directional_wick,
    _grade,
    _q,
)
from app.services.scanner_contract import EXPIRY_LIMITS
from app.services.scanner_scoring import ScannerEngine

__all__ = [
    "asyncio",
    "datetime",
    "timedelta",
    "Decimal",
    "MethodType",
    "Any",
    "pytest",
    "IndicatorPoint",
    "IndicatorSeries",
    "MarketStructure",
    "MarketCandle",
    "MarketCandleSeries",
    "MarketStatus",
    "CandidateLifecycle",
    "ScannerAuditRecord",
    "ScannerCandidate",
    "ScannerDirection",
    "ScannerGrade",
    "ScannerRunStatus",
    "ScannerSetup",
    "ScannerState",
    "UniverseCandidate",
    "UniverseSnapshot",
    "ScannerService",
    "EvaluationContext",
    "Frame",
    "ScannerEvaluationError",
    "_candidate_key",
    "_directional_break_margin",
    "_directional_close_position",
    "_directional_delta",
    "_directional_extreme",
    "_directional_histogram",
    "_directional_rsi_margin",
    "_directional_wick",
    "_grade",
    "_q",
    "EXPIRY_LIMITS",
    "ScannerEngine",
    "NOW",
    "candle",
    "frame",
    "universe",
    "base_context",
    "_prepare_setup",
    "FakeClock",
    "FakeMarket",
    "FakeIndicators",
    "FakeUniverse",
    "_series",
    "_candidate_for_service",
    "NoTimeMarket",
    "SkewMarket",
    "FailingUniverse",
]

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def candle(
    close: str = "100",
    *,
    open_: str = "99.5",
    high: str = "100.2",
    low: str = "99.4",
    minutes_ago: int = 0,
    interval_minutes: int = 15,
) -> MarketCandle:
    close_time = NOW - timedelta(minutes=minutes_ago)
    return MarketCandle(
        open_time=close_time - timedelta(minutes=interval_minutes) + timedelta(milliseconds=1),
        close_time=close_time,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1000"),
        quote_volume=Decimal("100000"),
        trades=100,
    )


def frame(
    close: str = "100",
    *,
    open_: str = "99.5",
    high: str = "100.2",
    low: str = "99.4",
    ema20: str = "99",
    ema50: str = "98",
    ema200: str = "95",
    rsi: str = "60",
    macd: str = "0.3",
    signal: str = "0.2",
    histogram: str = "0.1",
    atr: str = "1",
    volume_ratio: str = "1.5",
    minutes_ago: int = 0,
    interval_minutes: int = 15,
) -> Frame:
    item = candle(
        close,
        open_=open_,
        high=high,
        low=low,
        minutes_ago=minutes_ago,
        interval_minutes=interval_minutes,
    )
    point = IndicatorPoint(
        close_time=item.close_time,
        close=item.close,
        ema20=Decimal(ema20),
        ema50=Decimal(ema50),
        ema200=Decimal(ema200),
        rsi14=Decimal(rsi),
        macd=Decimal(macd),
        macd_signal=Decimal(signal),
        macd_histogram=Decimal(histogram),
        atr14=Decimal(atr),
        volume=item.volume,
        volume_sma20=Decimal("1000"),
        volume_ratio=Decimal(volume_ratio),
    )
    return Frame(item, point)


def universe() -> UniverseCandidate:
    return UniverseCandidate(
        rank=1,
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_volume=Decimal("1000000000"),
        bid_price=Decimal("100"),
        ask_price=Decimal("100.01"),
        spread_bps=Decimal("1"),
    )


def base_context(direction: ScannerDirection) -> EvaluationContext:
    h = [
        frame(
            "110" if direction is ScannerDirection.LONG else "90",
            ema20="105" if direction is ScannerDirection.LONG else "95",
            ema50="100",
            ema200="90" if direction is ScannerDirection.LONG else "110",
            rsi="60" if direction is ScannerDirection.LONG else "40",
            macd="1" if direction is ScannerDirection.LONG else "-1",
            signal="0",
            histogram="0.2" if direction is ScannerDirection.LONG else "-0.2",
            interval_minutes=60,
            minutes_ago=index * 60,
        )
        for index in range(30)
    ]
    s = [frame(minutes_ago=index * 15) for index in range(30)]
    e = [
        frame(
            interval_minutes=5,
            minutes_ago=index * 5,
            ema20="99",
            ema50="98",
            rsi="60",
            histogram="0.1",
            volume_ratio="1.5",
        )
        for index in range(10)
    ]
    return EvaluationContext(
        direction=direction,
        h=h,
        s=s,
        e=e,
        universe=universe(),
        exchange_time=NOW,
        counts={"1h": 250, "15m": 250, "5m": 250},
        freshness={"1h": Decimal("1"), "15m": Decimal("1"), "5m": Decimal("1")},
    )


def _prepare_setup(direction: ScannerDirection, setup: ScannerSetup) -> EvaluationContext:
    ctx = base_context(direction)
    d = direction
    if setup is ScannerSetup.TREND_PULLBACK:
        if d is ScannerDirection.LONG:
            ctx.s[3] = frame("103", minutes_ago=45)
            ctx.s[2] = frame("102", minutes_ago=30)
            ctx.s[1] = frame(
                "101",
                open_="101.2",
                high="101.5",
                low="98.5",
                ema20="100",
                ema50="99",
                rsi="50",
                histogram="0.05",
                volume_ratio="1",
                minutes_ago=15,
            )
            ctx.s[0] = frame(
                "102",
                open_="100",
                high="102.2",
                low="99.8",
                ema20="100",
                ema50="99",
                rsi="55",
                histogram="0.2",
                volume_ratio="1",
            )
        else:
            ctx.s[3] = frame("97", minutes_ago=45)
            ctx.s[2] = frame("98", minutes_ago=30)
            ctx.s[1] = frame(
                "99",
                open_="98.8",
                high="101.5",
                low="98.5",
                ema20="100",
                ema50="101",
                rsi="50",
                histogram="-0.05",
                volume_ratio="1",
                minutes_ago=15,
            )
            ctx.s[0] = frame(
                "98",
                open_="100",
                high="100.2",
                low="97.8",
                ema20="100",
                ema50="101",
                rsi="45",
                histogram="-0.2",
                volume_ratio="1",
            )
    elif setup is ScannerSetup.BREAKOUT_RETEST:
        prior = [
            frame("99", open_="98.8", high="100", low="98", minutes_ago=(index + 2) * 15)
            for index in range(20)
        ]
        ctx.s[2:22] = prior
        if d is ScannerDirection.LONG:
            ctx.s[1] = frame(
                "100.5", open_="99.5", high="100.6", low="99.4", volume_ratio="2", minutes_ago=15
            )
            ctx.s[0] = frame("100.2", open_="99.9", high="100.4", low="99.9", volume_ratio="1")
        else:
            for index in range(2, 22):
                ctx.s[index] = frame(
                    "101", open_="101.2", high="102", low="100", minutes_ago=index * 15
                )
            ctx.s[1] = frame(
                "99.5", open_="100.5", high="100.6", low="99.4", volume_ratio="2", minutes_ago=15
            )
            ctx.s[0] = frame("99.8", open_="100.1", high="100.1", low="99.6", volume_ratio="1")
    elif setup is ScannerSetup.EMA_REJECTION:
        if d is ScannerDirection.LONG:
            ctx.s[0] = frame(
                "100.1",
                open_="100",
                high="100.15",
                low="99.8",
                ema20="100",
                ema50="98",
                rsi="55",
                histogram="0.2",
                volume_ratio="1",
            )
            ctx.s[1] = frame(histogram="0.1", minutes_ago=15)
        else:
            ctx.s[0] = frame(
                "99.9",
                open_="100",
                high="100.2",
                low="99.85",
                ema20="100",
                ema50="102",
                rsi="45",
                histogram="-0.2",
                volume_ratio="1",
            )
            ctx.s[1] = frame(histogram="-0.1", minutes_ago=15)
    elif setup is ScannerSetup.LIQUIDITY_SWEEP_REVERSAL:
        for index in range(1, 11):
            ctx.s[index] = frame(
                "100.5", open_="100.4", high="101", low="100", minutes_ago=index * 15
            )
        if d is ScannerDirection.LONG:
            ctx.s[0] = frame(
                "100.1",
                open_="100.05",
                high="100.15",
                low="99.9",
                rsi="45",
                histogram="0.2",
                volume_ratio="1.2",
            )
            ctx.s[1] = frame(
                "100.5",
                open_="100.4",
                high="101",
                low="100",
                rsi="40",
                histogram="0.1",
                minutes_ago=15,
            )
        else:
            for index in range(1, 11):
                ctx.s[index] = frame(
                    "99.5", open_="99.6", high="100", low="99", minutes_ago=index * 15
                )
            ctx.s[0] = frame(
                "99.9",
                open_="99.95",
                high="100.1",
                low="99.85",
                rsi="55",
                histogram="-0.2",
                volume_ratio="1.2",
            )
            ctx.s[1] = frame(
                "99.5",
                open_="99.6",
                high="100",
                low="99",
                rsi="60",
                histogram="-0.1",
                minutes_ago=15,
            )
    else:
        if d is ScannerDirection.LONG:
            for index in range(1, 4):
                ctx.s[index] = frame(
                    f"{99.8 + index / 10}",
                    open_="99.7",
                    high="100.1",
                    low="99.5",
                    ema20="99",
                    ema50="98",
                    volume_ratio="1",
                    minutes_ago=index * 15,
                )
            ctx.s[0] = frame(
                "100.6",
                open_="100",
                high="100.7",
                low="99.9",
                ema20="99",
                ema50="98",
                rsi="60",
                histogram="0.2",
                volume_ratio="1.2",
            )
        else:
            for index in range(1, 4):
                ctx.s[index] = frame(
                    f"{100.2 - index / 10}",
                    open_="100.3",
                    high="100.5",
                    low="99.9",
                    ema20="101",
                    ema50="102",
                    volume_ratio="1",
                    minutes_ago=index * 15,
                )
            ctx.s[0] = frame(
                "99.4",
                open_="100",
                high="100.1",
                low="99.3",
                ema20="101",
                ema50="102",
                rsi="40",
                histogram="-0.2",
                volume_ratio="1.2",
            )
    return ctx


class FakeClock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class FakeMarket:
    async def status(self) -> MarketStatus:
        return MarketStatus(state="connected", checked_at=NOW, exchange_time=NOW)

    async def candles(self, symbol: str, interval: str, limit: int) -> MarketCandleSeries:
        step = {"1h": 60, "15m": 15, "5m": 5}[interval]
        items = [
            candle(
                close=str(100 + index / 100),
                open_=str(99.5 + index / 100),
                high=str(100.2 + index / 100),
                low=str(99.4 + index / 100),
                minutes_ago=(limit - 1 - index) * step,
                interval_minutes=step,
            )
            for index in range(limit)
        ]
        return MarketCandleSeries(symbol=symbol, interval=interval, fetched_at=NOW, candles=items)


class FakeIndicators:
    async def build(self, symbol: str, interval: str, limit: int) -> IndicatorSeries:
        series = await FakeMarket().candles(symbol, interval, limit)
        points = [
            IndicatorPoint(
                close_time=item.close_time,
                close=item.close,
                ema20=item.close - 1,
                ema50=item.close - 2,
                ema200=item.close - 5,
                rsi14=Decimal("60"),
                macd=Decimal("1"),
                macd_signal=Decimal("0"),
                macd_histogram=Decimal("0.2"),
                atr14=Decimal("1"),
                volume=item.volume,
                volume_sma20=Decimal("1000"),
                volume_ratio=Decimal("1.5"),
            )
            for item in series.candles
        ]
        return IndicatorSeries(
            symbol=symbol,
            interval=interval,
            generated_at=NOW,
            candle_count=limit,
            warmup_complete=True,
            structure=MarketStructure(state="bullish", lookback=20),
            points=points,
        )


class FakeUniverse:
    async def build(self) -> UniverseSnapshot:
        item = universe()
        return UniverseSnapshot(
            generated_at=NOW,
            max_symbols=50,
            min_quote_volume=Decimal("10000000"),
            max_spread_bps=Decimal("10"),
            eligible_count=1,
            rejected_count=0,
            candidates=[item],
            rejections=[],
        )


def _series(
    interval: str, count: int = 200, *, stale: bool = False
) -> tuple[MarketCandleSeries, IndicatorSeries]:
    step = {"1h": 60, "15m": 15, "5m": 5}[interval]
    candles = [
        candle(
            close=str(100 + index / 100),
            open_=str(99.5 + index / 100),
            high=str(100.2 + index / 100),
            low=str(99.4 + index / 100),
            minutes_ago=(count - 1 - index) * step,
            interval_minutes=step,
        )
        for index in range(count)
    ]
    points = [
        IndicatorPoint(
            close_time=item.close_time,
            close=item.close,
            ema20=item.close - 1,
            ema50=item.close - 2,
            ema200=item.close - 5,
            rsi14=Decimal("60"),
            macd=Decimal("1"),
            macd_signal=Decimal("0"),
            macd_histogram=Decimal("0.2"),
            atr14=Decimal("1"),
            volume=item.volume,
            volume_sma20=Decimal("1000"),
            volume_ratio=Decimal("1.5"),
        )
        for item in candles
    ]
    return (
        MarketCandleSeries(
            symbol="BTCUSDT",
            interval=interval,
            fetched_at=NOW,
            stale=stale,
            candles=candles,
        ),
        IndicatorSeries(
            symbol="BTCUSDT",
            interval=interval,
            generated_at=NOW,
            candle_count=count,
            warmup_complete=count >= 200,
            stale=stale,
            structure=MarketStructure(state="bullish", lookback=20),
            points=points,
        ),
    )


def _candidate_for_service(
    *,
    lifecycle: CandidateLifecycle = CandidateLifecycle.WATCH_NEAR,
    expires_at: datetime | None = None,
) -> ScannerCandidate:
    return ScannerCandidate(
        candidate_id="b" * 64,
        symbol="BTCUSDT",
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        reference_close_time=NOW - timedelta(minutes=15),
        setup_confirmed_at=NOW - timedelta(minutes=15),
        expires_at=expires_at or NOW + timedelta(minutes=45),
        qualification_expires_at=(
            NOW + timedelta(minutes=15) if lifecycle is CandidateLifecycle.QUALIFIED else None
        ),
        lifecycle=lifecycle,
        score=85,
        confidence=75,
        grade=ScannerGrade.A,
        entry_ready=lifecycle is CandidateLifecycle.QUALIFIED,
        universe_rank=1,
        quote_volume=Decimal("1000000000"),
        spread_bps=Decimal("1"),
        level=Decimal("100"),
        entry_trigger_price=Decimal("101"),
        evaluated_at=NOW,
        evidence={
            "reference_extreme": "99",
            "pullback_swing_low": "99",
            "pullback_swing_high": "101",
            "reference_low": "99",
            "reference_high": "101",
        },
        score_components={"setup": Decimal("20")},
    )


class NoTimeMarket(FakeMarket):
    async def status(self) -> MarketStatus:
        return MarketStatus(state="unavailable", checked_at=NOW, exchange_time=None)


class SkewMarket(FakeMarket):
    async def status(self) -> MarketStatus:
        return MarketStatus(
            state="connected", checked_at=NOW, exchange_time=NOW + timedelta(seconds=6)
        )


class FailingUniverse(FakeUniverse):
    async def build(self) -> UniverseSnapshot:
        raise ValueError("down")
