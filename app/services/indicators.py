"""Deterministic closed-candle Indicator Engine."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from app.schemas.indicators import IndicatorPoint, IndicatorSeries, MarketStructure
from app.schemas.market import MarketCandle, MarketCandleSeries

_EMA_PERIODS = (20, 50, 200)
_RSI_PERIOD = 14
_ATR_PERIOD = 14
_MACD_FAST = 12
_MACD_SLOW = 26
_MACD_SIGNAL = 9
_VOLUME_PERIOD = 20
_STRUCTURE_LOOKBACK = 20
_WARMUP_REQUIRED = 200


class CandleProvider(Protocol):
    """Closed-candle source required by the Indicator Engine."""

    async def candles(self, symbol: str, interval: str, limit: int) -> MarketCandleSeries: ...


def _average(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


def _sma(values: list[Decimal], period: int) -> list[Decimal | None]:
    result: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return result
    rolling = sum(values[:period], Decimal("0"))
    result[period - 1] = rolling / Decimal(period)
    for index in range(period, len(values)):
        rolling += values[index] - values[index - period]
        result[index] = rolling / Decimal(period)
    return result


def _ema(values: list[Decimal], period: int) -> list[Decimal | None]:
    result: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return result
    alpha = Decimal("2") / Decimal(period + 1)
    current = _average(values[:period])
    result[period - 1] = current
    for index in range(period, len(values)):
        current = ((values[index] - current) * alpha) + current
        result[index] = current
    return result


def _ema_sparse(values: list[Decimal | None], period: int) -> list[Decimal | None]:
    indexes = [index for index, value in enumerate(values) if value is not None]
    dense = [values[index] for index in indexes]
    dense_values = [value for value in dense if value is not None]
    dense_ema = _ema(dense_values, period)
    result: list[Decimal | None] = [None] * len(values)
    for index, value in zip(indexes, dense_ema, strict=True):
        result[index] = value
    return result


def _rsi(closes: list[Decimal], period: int) -> list[Decimal | None]:
    result: list[Decimal | None] = [None] * len(closes)
    if len(closes) <= period:
        return result
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    gains = [max(change, Decimal("0")) for change in changes]
    losses = [max(-change, Decimal("0")) for change in changes]
    average_gain = _average(gains[:period])
    average_loss = _average(losses[:period])

    def value(gain: Decimal, loss: Decimal) -> Decimal:
        if loss == 0:
            return Decimal("100") if gain > 0 else Decimal("50")
        relative_strength = gain / loss
        return Decimal("100") - (Decimal("100") / (Decimal("1") + relative_strength))

    result[period] = value(average_gain, average_loss)
    for close_index in range(period + 1, len(closes)):
        change_index = close_index - 1
        average_gain = ((average_gain * Decimal(period - 1)) + gains[change_index]) / Decimal(
            period
        )
        average_loss = ((average_loss * Decimal(period - 1)) + losses[change_index]) / Decimal(
            period
        )
        result[close_index] = value(average_gain, average_loss)
    return result


def _true_ranges(candles: list[MarketCandle]) -> list[Decimal]:
    if not candles:
        return []
    result = [candles[0].high - candles[0].low]
    for index in range(1, len(candles)):
        candle = candles[index]
        previous_close = candles[index - 1].close
        result.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
    return result


def _wilder_average(values: list[Decimal], period: int) -> list[Decimal | None]:
    result: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = _average(values[:period])
    result[period - 1] = current
    for index in range(period, len(values)):
        current = ((current * Decimal(period - 1)) + values[index]) / Decimal(period)
        result[index] = current
    return result


def _vwap(candles: list[MarketCandle]) -> list[Decimal | None]:
    result: list[Decimal | None] = []
    cumulative_value = Decimal("0")
    cumulative_volume = Decimal("0")
    for candle in candles:
        typical_price = (candle.high + candle.low + candle.close) / Decimal("3")
        cumulative_value += typical_price * candle.volume
        cumulative_volume += candle.volume
        result.append(cumulative_value / cumulative_volume if cumulative_volume > 0 else None)
    return result


def _market_structure(candles: list[MarketCandle]) -> MarketStructure:
    available = candles[-_STRUCTURE_LOOKBACK:]
    support = min((candle.low for candle in available), default=None)
    resistance = max((candle.high for candle in available), default=None)
    if len(available) < _STRUCTURE_LOOKBACK:
        return MarketStructure(
            state="insufficient_data",
            lookback=_STRUCTURE_LOOKBACK,
            support=support,
            resistance=resistance,
        )

    half = _STRUCTURE_LOOKBACK // 2
    previous = available[:half]
    recent = available[half:]
    previous_high = max(candle.high for candle in previous)
    previous_low = min(candle.low for candle in previous)
    recent_high = max(candle.high for candle in recent)
    recent_low = min(candle.low for candle in recent)
    if recent_high > previous_high and recent_low > previous_low:
        state = "bullish"
    elif recent_high < previous_high and recent_low < previous_low:
        state = "bearish"
    else:
        state = "range"
    return MarketStructure(
        state=state,
        lookback=_STRUCTURE_LOOKBACK,
        support=support,
        resistance=resistance,
        previous_high=previous_high,
        previous_low=previous_low,
        recent_high=recent_high,
        recent_low=recent_low,
    )


class IndicatorEngine:
    """Calculate indicator series from normalized closed candles."""

    def calculate(self, series: MarketCandleSeries) -> IndicatorSeries:
        candles = series.candles
        closes = [candle.close for candle in candles]
        volumes = [candle.volume for candle in candles]
        ema20 = _ema(closes, _EMA_PERIODS[0])
        ema50 = _ema(closes, _EMA_PERIODS[1])
        ema200 = _ema(closes, _EMA_PERIODS[2])
        vwap = _vwap(candles)
        rsi14 = _rsi(closes, _RSI_PERIOD)
        atr14 = _wilder_average(_true_ranges(candles), _ATR_PERIOD)
        macd_fast = _ema(closes, _MACD_FAST)
        macd_slow = _ema(closes, _MACD_SLOW)
        macd: list[Decimal | None] = [
            fast - slow if fast is not None and slow is not None else None
            for fast, slow in zip(macd_fast, macd_slow, strict=True)
        ]
        macd_signal = _ema_sparse(macd, _MACD_SIGNAL)
        macd_histogram: list[Decimal | None] = [
            value - signal if value is not None and signal is not None else None
            for value, signal in zip(macd, macd_signal, strict=True)
        ]
        volume_sma20 = _sma(volumes, _VOLUME_PERIOD)

        points: list[IndicatorPoint] = []
        for index, candle in enumerate(candles):
            volume_average = volume_sma20[index]
            volume_ratio = (
                candle.volume / volume_average
                if volume_average is not None and volume_average > 0
                else None
            )
            points.append(
                IndicatorPoint(
                    close_time=candle.close_time,
                    close=candle.close,
                    ema20=ema20[index],
                    ema50=ema50[index],
                    ema200=ema200[index],
                    vwap=vwap[index],
                    rsi14=rsi14[index],
                    macd=macd[index],
                    macd_signal=macd_signal[index],
                    macd_histogram=macd_histogram[index],
                    atr14=atr14[index],
                    volume=candle.volume,
                    volume_sma20=volume_average,
                    volume_ratio=volume_ratio,
                )
            )

        return IndicatorSeries(
            symbol=series.symbol,
            interval=series.interval,
            generated_at=datetime.now(UTC),
            candle_count=len(candles),
            warmup_required=_WARMUP_REQUIRED,
            warmup_complete=len(candles) >= _WARMUP_REQUIRED,
            stale=series.stale,
            cache_age_seconds=series.cache_age_seconds,
            structure=_market_structure(candles),
            points=points,
        )


class IndicatorService:
    """Load closed candles and calculate deterministic indicators."""

    def __init__(self, candle_provider: CandleProvider) -> None:
        self._candle_provider = candle_provider
        self._engine = IndicatorEngine()

    async def build(self, symbol: str, interval: str, limit: int) -> IndicatorSeries:
        series = await self._candle_provider.candles(symbol, interval, limit)
        return self._engine.calculate(series)
