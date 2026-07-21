"""Typed Universe Engine contracts."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

type UniverseRejectionCode = Literal[
    "invalid_symbol_metadata",
    "non_usdt_quote",
    "non_perpetual_contract",
    "not_trading",
    "missing_ticker",
    "invalid_quote_volume",
    "below_min_quote_volume",
    "missing_book_ticker",
    "invalid_book_ticker",
    "spread_too_wide",
    "universe_limit",
    "trend_sideways",
    "trend_mixed",
    "trend_not_directional",
    "trend_prefilter_failed",
    "directional_universe_limit",
]


class UniverseCandidate(BaseModel):
    """One eligible and ranked USD-M perpetual contract."""

    rank: int = Field(ge=1)
    symbol: str
    base_asset: str
    quote_asset: Literal["USDT"] = "USDT"
    quote_volume: Decimal = Field(ge=0)
    bid_price: Decimal = Field(gt=0)
    ask_price: Decimal = Field(gt=0)
    spread_bps: Decimal = Field(ge=0)


class UniverseRejection(BaseModel):
    """Auditable reason a symbol was excluded from the active universe."""

    symbol: str
    code: UniverseRejectionCode
    detail: str


class UniverseSnapshot(BaseModel):
    """Deterministic eligible universe and rejection audit."""

    source: Literal["binance_usdm_public"] = "binance_usdm_public"
    generated_at: datetime
    max_symbols: int = Field(ge=1)
    min_quote_volume: Decimal = Field(ge=0)
    max_spread_bps: Decimal = Field(gt=0)
    eligible_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    candidates: list[UniverseCandidate]
    rejections: list[UniverseRejection]
