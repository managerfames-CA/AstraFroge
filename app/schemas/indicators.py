"""Typed Indicator Engine contracts."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

type IndicatorInterval = Literal["5m", "15m", "1h"]
type MarketStructureState = Literal["bullish", "bearish", "range", "insufficient_data"]


class IndicatorPoint(BaseModel):
    """Indicators aligned to one closed market candle."""

    close_time: datetime
    close: Decimal
    ema20: Decimal | None = None
    ema50: Decimal | None = None
    ema200: Decimal | None = None
    vwap: Decimal | None = None
    rsi14: Decimal | None = None
    macd: Decimal | None = None
    macd_signal: Decimal | None = None
    macd_histogram: Decimal | None = None
    atr14: Decimal | None = None
    volume: Decimal = Field(ge=0)
    volume_sma20: Decimal | None = Field(default=None, ge=0)
    volume_ratio: Decimal | None = Field(default=None, ge=0)


class MarketStructure(BaseModel):
    """Deterministic recent high/low structure summary."""

    state: MarketStructureState
    lookback: int = Field(ge=1)
    support: Decimal | None = None
    resistance: Decimal | None = None
    previous_high: Decimal | None = None
    previous_low: Decimal | None = None
    recent_high: Decimal | None = None
    recent_low: Decimal | None = None


class IndicatorSeries(BaseModel):
    """Versioned closed-candle indicator output with freshness and warm-up metadata."""

    symbol: str
    interval: IndicatorInterval
    source: Literal["binance_usdm_public"] = "binance_usdm_public"
    generated_at: datetime
    candle_count: int = Field(ge=0)
    warmup_required: int = Field(default=200, ge=1)
    warmup_complete: bool
    stale: bool = False
    cache_age_seconds: float = Field(default=0, ge=0)
    source_candle_version: str | None = None
    indicator_engine_version: str | None = None
    snapshot_version: str | None = None
    cache_hit: bool = False
    structure: MarketStructure
    points: list[IndicatorPoint]
