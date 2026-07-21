"""Typed public market-data contracts."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

type MarketDataState = Literal["connected", "degraded", "unavailable"]


class MarketStatus(BaseModel):
    """Factual state of the public Binance market-data adapter."""

    state: MarketDataState
    source: Literal["binance_usdm_public"] = "binance_usdm_public"
    checked_at: datetime
    exchange_time: datetime | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    detail: str | None = None


class MarketSymbol(BaseModel):
    """Tradable USD-M Futures symbol metadata."""

    symbol: str
    base_asset: str
    quote_asset: str
    contract_type: str
    status: str
    price_precision: int = Field(ge=0)
    quantity_precision: int = Field(ge=0)


class MarketTicker(BaseModel):
    """Current public 24-hour ticker snapshot."""

    symbol: str
    last_price: Decimal
    price_change_percent: Decimal
    high_price: Decimal
    low_price: Decimal
    quote_volume: Decimal
    close_time: datetime
    fetched_at: datetime
    stale: bool = False
    cache_age_seconds: float = Field(default=0, ge=0)


class MarketCandle(BaseModel):
    """One normalized closed OHLCV candle."""

    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trades: int = Field(ge=0)
    closed: Literal[True] = True


class MarketCandleSeries(BaseModel):
    """Versioned closed-candle snapshot with source and freshness metadata."""

    symbol: str
    interval: Literal["5m", "15m", "1h"]
    source: Literal["binance_usdm_public"] = "binance_usdm_public"
    fetched_at: datetime
    stale: bool = False
    cache_age_seconds: float = Field(default=0, ge=0)
    last_closed_candle_time: datetime | None = None
    candle_count: int = Field(default=0, ge=0)
    data_version: str | None = None
    snapshot_version: str | None = None
    cache_hit: bool = False
    candles: list[MarketCandle]
