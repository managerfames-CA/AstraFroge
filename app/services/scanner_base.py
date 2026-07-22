"""Scanner Contract Version 1 mathematical helpers and mandatory base gates."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.schemas.indicators import IndicatorPoint, IndicatorSeries
from app.schemas.market import MarketCandle, MarketCandleSeries
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.universe import UniverseCandidate
from app.services.scanner_contract import (
    FRESHNESS_LIMITS,
    FUTURE_CANDLE_TOLERANCE,
    MINIMUM_CANDLES,
    TIMEFRAME_INTERVAL,
    VOLATILITY_LIMITS,
)

D0 = Decimal("0")
D1 = Decimal("1")
D100 = Decimal("100")
_REQUIRED_DEPTH = {"1h": 4, "15m": 24, "5m": 3}


@dataclass(frozen=True)
class Frame:
    """One aligned closed candle and indicator point."""

    candle: MarketCandle
    indicator: IndicatorPoint


@dataclass(frozen=True)
class SetupMatch:
    """Accepted deterministic 15M setup evidence."""

    setup: ScannerSetup
    reference_close_time: datetime
    setup_confirmed_at: datetime
    expires_at: datetime
    level: Decimal | None
    selected_ema: Decimal | None
    entry_trigger_price: Decimal
    setup_points: Decimal
    accepted_reasons: tuple[str, ...]
    evidence: dict[str, Any]


@dataclass(frozen=True)
class EvaluationContext:
    """Validated multi-timeframe inputs for one symbol and direction."""

    direction: ScannerDirection
    h: list[Frame]
    s: list[Frame]
    e: list[Frame]
    universe: UniverseCandidate
    exchange_time: datetime
    counts: dict[str, int]
    freshness: dict[str, Decimal]


class ScannerEvaluationError(Exception):
    """Closed deterministic evaluation failure."""

    def __init__(self, code: str, detail: str, timeframe: str | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.timeframe = timeframe


def _q(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _clamp(value: Decimal, lower: Decimal = D0, upper: Decimal = D1) -> Decimal:
    return min(upper, max(lower, value))


def _n_up(value: Decimal, minimum: Decimal, full: Decimal) -> Decimal:
    if full <= minimum:
        raise ValueError("N_UP requires full > minimum")
    if value <= minimum:
        return D0
    if value >= full:
        return D1
    return (value - minimum) / (full - minimum)


def _n_down(value: Decimal, full: Decimal, maximum: Decimal) -> Decimal:
    if maximum <= full:
        raise ValueError("N_DOWN requires maximum > full")
    if value <= full:
        return D1
    if value >= maximum:
        return D0
    return D1 - ((value - full) / (maximum - full))


def _n_target(value: Decimal, target: Decimal, tolerance: Decimal) -> Decimal:
    if tolerance <= 0:
        raise ValueError("N_TARGET requires positive tolerance")
    return _clamp(D1 - (abs(value - target) / tolerance))


def _body(candle: MarketCandle) -> Decimal:
    return abs(candle.close - candle.open)


def _range(candle: MarketCandle) -> Decimal:
    return candle.high - candle.low


def _lower_wick(candle: MarketCandle) -> Decimal:
    return min(candle.open, candle.close) - candle.low


def _upper_wick(candle: MarketCandle) -> Decimal:
    return candle.high - max(candle.open, candle.close)


def _directional_wick(candle: MarketCandle, direction: ScannerDirection) -> Decimal:
    return _lower_wick(candle) if direction is ScannerDirection.LONG else _upper_wick(candle)


def _directional_close_position(
    candle: MarketCandle, direction: ScannerDirection
) -> Decimal:
    candle_range = _range(candle)
    if candle_range <= 0:
        raise ScannerEvaluationError("INVALID_15M_OHLCV", "Candle range must be positive")
    if direction is ScannerDirection.LONG:
        return (candle.close - candle.low) / candle_range
    return (candle.high - candle.close) / candle_range


def _directional_extreme(candle: MarketCandle, direction: ScannerDirection) -> Decimal:
    return candle.low if direction is ScannerDirection.LONG else candle.high


def _directional_break_margin(
    price: Decimal, level: Decimal, direction: ScannerDirection
) -> Decimal:
    return price - level if direction is ScannerDirection.LONG else level - price


def _directional_reclaim_margin(
    close: Decimal, level: Decimal, direction: ScannerDirection
) -> Decimal:
    return close - level if direction is ScannerDirection.LONG else level - close


def _directional_ema_extension(
    close: Decimal, ema: Decimal, direction: ScannerDirection
) -> Decimal:
    return close - ema if direction is ScannerDirection.LONG else ema - close


def _directional_histogram(value: Decimal, direction: ScannerDirection) -> Decimal:
    return value if direction is ScannerDirection.LONG else -value


def _directional_delta(
    current: Decimal, previous: Decimal, direction: ScannerDirection
) -> Decimal:
    return current - previous if direction is ScannerDirection.LONG else previous - current


def _directional_rsi_margin(value: Decimal, direction: ScannerDirection) -> Decimal:
    if direction is ScannerDirection.LONG:
        return value - Decimal("50")
    return Decimal("50") - value


def _directional_sweep_depth(
    candle: MarketCandle, level: Decimal, direction: ScannerDirection
) -> Decimal:
    return level - candle.low if direction is ScannerDirection.LONG else candle.high - level


def _directional_previous_break_level(
    candle: MarketCandle, direction: ScannerDirection
) -> Decimal:
    return candle.high if direction is ScannerDirection.LONG else candle.low


def _directional_compression_boundary(
    high: Decimal, low: Decimal, direction: ScannerDirection
) -> Decimal:
    return high if direction is ScannerDirection.LONG else low


def _required(value: Decimal | None, name: str) -> Decimal:
    if value is None or not value.is_finite():
        raise ScannerEvaluationError("MISSING_REQUIRED_INDICATOR", f"{name} is unavailable")
    return value


def _frame_value(frame: Frame, name: str) -> Decimal:
    return _required(getattr(frame.indicator, name), name)


def _candidate_key(
    symbol: str,
    direction: ScannerDirection,
    setup: ScannerSetup,
    reference_close_time: datetime,
) -> str:
    timestamp = reference_close_time.astimezone(UTC).isoformat()
    raw = f"{symbol.upper()}|{direction.value}|{setup.value}|15m|{timestamp}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _grade(score: int) -> ScannerGrade:
    if score >= 90:
        return ScannerGrade.A_PLUS
    if score >= 85:
        return ScannerGrade.A
    if score >= 80:
        return ScannerGrade.B_PLUS
    return ScannerGrade.REJECT


def _finite(values: tuple[Decimal, ...]) -> bool:
    return all(value.is_finite() for value in values)


class ScannerEngineBase:
    """Mandatory integrity, regime, volatility, and entry-readiness gates."""

    @staticmethod
    def align(
        candles: MarketCandleSeries,
        indicators: IndicatorSeries,
        *,
        exchange_time: datetime,
    ) -> tuple[list[Frame], Decimal]:
        interval = candles.interval
        label = {"1h": "1H", "15m": "15M", "5m": "5M"}[interval]
        if not candles.candles:
            raise ScannerEvaluationError(
                f"MISSING_{label}_CANDLES", f"{interval} candles are unavailable", interval
            )
        if candles.stale or indicators.stale:
            raise ScannerEvaluationError(f"STALE_{label}_DATA", "Series is stale", interval)
        if len(candles.candles) < MINIMUM_CANDLES:
            raise ScannerEvaluationError(
                f"INSUFFICIENT_{label}_HISTORY",
                f"{interval} requires {MINIMUM_CANDLES} closed candles",
                interval,
            )
        if not indicators.warmup_complete or len(indicators.points) < MINIMUM_CANDLES:
            raise ScannerEvaluationError(
                f"INSUFFICIENT_{label}_HISTORY",
                f"{interval} indicator warm-up is incomplete",
                interval,
            )
        candle_times = [item.close_time for item in candles.candles]
        point_times = [item.close_time for item in indicators.points]
        if len(candle_times) != len(set(candle_times)) or candle_times != sorted(candle_times):
            raise ScannerEvaluationError(
                f"INVALID_{label}_OHLCV", "Candle timestamps are duplicated or unordered", interval
            )
        if len(point_times) != len(set(point_times)) or point_times != sorted(point_times):
            raise ScannerEvaluationError(
                "INDICATOR_CALCULATION_FAILED", "Indicator timestamps are duplicated or unordered"
            )
        candle_map = {item.close_time: item for item in candles.candles}
        point_map = {item.close_time: item for item in indicators.points}
        common = sorted(set(candle_map) & set(point_map))
        if len(common) < MINIMUM_CANDLES:
            raise ScannerEvaluationError(
                f"MISSING_{label}_CANDLES",
                "Candle and indicator timestamps do not align",
                interval,
            )
        ordered = [candle_map[stamp] for stamp in common]
        expected = TIMEFRAME_INTERVAL[interval]
        for index, candle in enumerate(ordered):
            numeric = (
                candle.open,
                candle.high,
                candle.low,
                candle.close,
                candle.volume,
                candle.quote_volume,
            )
            if (
                not _finite(numeric)
                or candle.open <= 0
                or candle.high <= 0
                or candle.low <= 0
                or candle.close <= 0
                or candle.high < max(candle.open, candle.close)
                or candle.low > min(candle.open, candle.close)
                or candle.high < candle.low
                or candle.volume < 0
                or candle.quote_volume < 0
                or candle.trades < 0
                or candle.open_time >= candle.close_time
                or candle.close_time > exchange_time + FUTURE_CANDLE_TOLERANCE
            ):
                raise ScannerEvaluationError(
                    f"INVALID_{label}_OHLCV", "Invalid OHLCV or timestamp", interval
                )
            if index and candle.close_time - ordered[index - 1].close_time != expected:
                raise ScannerEvaluationError(
                    f"INVALID_{label}_OHLCV", "Missing or duplicate candle interval", interval
                )
        latest = ordered[-1]
        age = max(timedelta(0), exchange_time - latest.close_time)
        limit = FRESHNESS_LIMITS[interval]
        if age > limit:
            raise ScannerEvaluationError(
                f"STALE_{label}_DATA",
                f"Latest candle age {age.total_seconds()} exceeds {limit.total_seconds()}",
                interval,
            )
        frames = [Frame(candle_map[stamp], point_map[stamp]) for stamp in reversed(common)]
        for frame in frames[: _REQUIRED_DEPTH[interval]]:
            for field in (
                "ema20",
                "ema50",
                "ema200",
                "rsi14",
                "macd",
                "macd_signal",
                "macd_histogram",
                "atr14",
                "volume_sma20",
                "volume_ratio",
            ):
                _required(getattr(frame.indicator, field), field)
            if _frame_value(frame, "atr14") <= 0 or _frame_value(frame, "volume_sma20") <= 0:
                raise ScannerEvaluationError(
                    "MISSING_REQUIRED_INDICATOR",
                    "ATR and volume SMA must be positive",
                    interval,
                )
        seconds = Decimal(str(age.total_seconds()))
        maximum = Decimal(str(limit.total_seconds()))
        return frames, _clamp(D1 - (seconds / maximum))

    @staticmethod
    def regime(h: list[Frame], structure_state: str) -> ScannerDirection:
        if structure_state == "insufficient_data":
            raise ScannerEvaluationError(
                "STRUCTURE_INSUFFICIENT", "1H market structure is unavailable", "1h"
            )
        h0, h3 = h[0], h[3]
        atr = _frame_value(h0, "atr14")
        ema20 = _frame_value(h0, "ema20")
        ema50 = _frame_value(h0, "ema50")
        ema200 = _frame_value(h0, "ema200")
        rsi = _frame_value(h0, "rsi14")
        macd = _frame_value(h0, "macd")
        signal = _frame_value(h0, "macd_signal")
        histogram = _frame_value(h0, "macd_histogram")
        sideways = (
            structure_state == "range"
            or abs(ema20 - ema50) <= Decimal("0.25") * atr
            or (
                Decimal("45") <= rsi <= Decimal("55")
                and abs(histogram) <= Decimal("0.05") * atr
            )
        )
        if sideways:
            raise ScannerEvaluationError("TREND_SIDEWAYS", "1H regime is SIDEWAYS", "1h")
        bullish = (
            h0.candle.close > ema20 > ema50 > ema200
            and ema20 > _frame_value(h3, "ema20")
            and ema50 > _frame_value(h3, "ema50")
            and ema200 >= _frame_value(h3, "ema200")
            and structure_state == "bullish"
            and Decimal("55") <= rsi <= Decimal("80")
            and macd > signal
            and histogram > 0
        )
        bearish = (
            h0.candle.close < ema20 < ema50 < ema200
            and ema20 < _frame_value(h3, "ema20")
            and ema50 < _frame_value(h3, "ema50")
            and ema200 <= _frame_value(h3, "ema200")
            and structure_state == "bearish"
            and Decimal("20") <= rsi <= Decimal("45")
            and macd < signal
            and histogram < 0
        )
        if bullish:
            return ScannerDirection.LONG
        if bearish:
            return ScannerDirection.SHORT
        raise ScannerEvaluationError("TREND_MIXED", "1H regime is MIXED", "1h")

    @staticmethod
    def selected_ema(s0: Frame, direction: ScannerDirection) -> Decimal:
        atr = _frame_value(s0, "atr14")
        if atr <= 0:
            raise ScannerEvaluationError("MISSING_REQUIRED_INDICATOR", "ATR must be positive")
        extreme = _directional_extreme(s0.candle, direction)
        ema20 = _frame_value(s0, "ema20")
        ema50 = _frame_value(s0, "ema50")
        distances = [(abs(extreme - ema20), 0, ema20), (abs(extreme - ema50), 1, ema50)]
        eligible = [item for item in distances if item[0] <= Decimal("0.20") * atr]
        if not eligible:
            raise ScannerEvaluationError(
                "EMA_REJECTION_NOT_CONFIRMED", "No EMA is eligible for rejection"
            )
        return min(eligible, key=lambda item: (item[0], item[1]))[2]

    @staticmethod
    def volatility(frame: Frame, interval: str) -> None:
        atr = _frame_value(frame, "atr14")
        if atr <= 0 or frame.candle.close <= 0:
            raise ScannerEvaluationError("MISSING_REQUIRED_INDICATOR", "ATR must be positive")
        ratio = atr / frame.candle.close
        minimum, maximum = VOLATILITY_LIMITS[interval]
        if ratio < minimum:
            raise ScannerEvaluationError(
                "VOLATILITY_BELOW_MINIMUM", f"{interval} ATR ratio too low", interval
            )
        if ratio > maximum:
            raise ScannerEvaluationError(
                "VOLATILITY_ABOVE_MAXIMUM", f"{interval} ATR ratio too high", interval
            )

    @staticmethod
    def shared_entry(
        e: list[Frame], direction: ScannerDirection, trigger: Decimal
    ) -> bool:
        e0, e1 = e[0], e[1]
        atr = _frame_value(e0, "atr14")
        ema20 = _frame_value(e0, "ema20")
        ema50 = _frame_value(e0, "ema50")
        rsi = _frame_value(e0, "rsi14")
        macd = _frame_value(e0, "macd")
        signal = _frame_value(e0, "macd_signal")
        histogram = _frame_value(e0, "macd_histogram")
        prior_histogram = _frame_value(e1, "macd_histogram")
        volume_ratio = _frame_value(e0, "volume_ratio")
        close_position = _directional_close_position(e0.candle, direction)
        if direction is ScannerDirection.LONG:
            return (
                e0.candle.close > trigger
                and e0.candle.close > ema20
                and ema20 >= ema50
                and e0.candle.close > e1.candle.high
                and e0.candle.close > e0.candle.open
                and close_position >= Decimal("0.65")
                and Decimal("52") <= rsi <= Decimal("72")
                and macd > signal
                and histogram > 0
                and histogram >= prior_histogram
                and volume_ratio >= Decimal("1.10")
                and e0.candle.close - ema20 <= Decimal("0.75") * atr
            )
        return (
            e0.candle.close < trigger
            and e0.candle.close < ema20
            and ema20 <= ema50
            and e0.candle.close < e1.candle.low
            and e0.candle.close < e0.candle.open
            and close_position >= Decimal("0.65")
            and Decimal("28") <= rsi <= Decimal("48")
            and macd < signal
            and histogram < 0
            and histogram <= prior_histogram
            and volume_ratio >= Decimal("1.10")
            and ema20 - e0.candle.close <= Decimal("0.75") * atr
        )
