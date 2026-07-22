"""Indicator Engine deterministic unit tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.schemas.market import MarketCandle, MarketCandleSeries
from app.services.indicators import IndicatorEngine


def _series(
    closes: list[Decimal],
    *,
    volumes: list[Decimal] | None = None,
    stale: bool = False,
) -> MarketCandleSeries:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    resolved_volumes = volumes or [Decimal("10")] * len(closes)
    candles = [
        MarketCandle(
            open_time=start + timedelta(minutes=5 * index),
            close_time=start + timedelta(minutes=5 * (index + 1)),
            open=close - Decimal("0.5"),
            high=close + Decimal("1"),
            low=close - Decimal("1"),
            close=close,
            volume=resolved_volumes[index],
            quote_volume=resolved_volumes[index] * close,
            trades=100,
        )
        for index, close in enumerate(closes)
    ]
    return MarketCandleSeries(
        symbol="BTCUSDT",
        interval="5m",
        fetched_at=start,
        stale=stale,
        cache_age_seconds=4.5 if stale else 0,
        candles=candles,
    )


def test_rising_series_calculates_full_indicator_set() -> None:
    closes = [Decimal(index) for index in range(1, 251)]
    volumes = [Decimal(100 + index) for index in range(250)]

    result = IndicatorEngine().calculate(_series(closes, volumes=volumes))
    latest = result.points[-1]

    assert result.candle_count == 250
    assert result.warmup_complete is True
    assert latest.ema20 is not None
    assert latest.ema50 is not None
    assert latest.ema200 is not None
    assert latest.ema20 > latest.ema50 > latest.ema200
    assert latest.vwap is not None
    assert latest.rsi14 == Decimal("100")
    assert latest.macd is not None
    assert latest.macd > 0
    assert latest.macd_signal is not None
    assert latest.macd_signal > 0
    assert latest.macd_histogram is not None
    assert latest.atr14 == Decimal("2")
    assert latest.volume_sma20 is not None
    assert latest.volume_ratio is not None
    assert latest.volume_ratio > 1
    assert result.structure.state == "bullish"
    assert result.structure.recent_high is not None
    assert result.structure.previous_high is not None
    assert result.structure.recent_high > result.structure.previous_high


def test_flat_series_has_neutral_rsi_and_zero_macd() -> None:
    closes = [Decimal("100")] * 60

    result = IndicatorEngine().calculate(_series(closes))
    latest = result.points[-1]

    assert result.warmup_complete is False
    assert latest.ema20 == Decimal("100")
    assert latest.ema50 == Decimal("100")
    assert latest.ema200 is None
    assert latest.vwap == Decimal("100")
    assert latest.rsi14 == Decimal("50")
    assert latest.macd == Decimal("0")
    assert latest.macd_signal == Decimal("0")
    assert latest.macd_histogram == Decimal("0")
    assert latest.atr14 == Decimal("2")
    assert latest.volume_sma20 == Decimal("10")
    assert latest.volume_ratio == Decimal("1")
    assert result.structure.state == "range"


def test_short_series_reports_incomplete_warmup() -> None:
    closes = [Decimal(index) for index in range(1, 11)]

    result = IndicatorEngine().calculate(_series(closes))
    latest = result.points[-1]

    assert result.warmup_complete is False
    assert result.warmup_required == 200
    assert latest.ema20 is None
    assert latest.rsi14 is None
    assert latest.macd is None
    assert latest.atr14 is None
    assert result.structure.state == "insufficient_data"
    assert result.structure.support == Decimal("0")
    assert result.structure.resistance == Decimal("11")


def test_freshness_metadata_is_propagated() -> None:
    result = IndicatorEngine().calculate(
        _series([Decimal("100")] * 20, stale=True)
    )

    assert result.stale is True
    assert result.cache_age_seconds == 4.5
