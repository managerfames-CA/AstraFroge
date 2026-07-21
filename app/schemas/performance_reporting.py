"""Typed BE-15 performance reporting contracts."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.journal_performance import JournalPerformanceState, JournalPnlSource


class PerformanceDimension(StrEnum):
    """Supported closed-trade report dimensions."""

    STRATEGY = "STRATEGY"
    SYMBOL = "SYMBOL"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


class PerformanceReportRow(BaseModel):
    """One aggregate derived only from verified closed Journal trades."""

    dimension: PerformanceDimension
    key: str
    label: str
    period_started_on: date | None = None
    period_ended_on: date | None = None
    closed_trade_count: int = Field(ge=0)
    winning_trades: int = Field(ge=0)
    losing_trades: int = Field(ge=0)
    breakeven_trades: int = Field(ge=0)
    win_rate_percent: Decimal = Field(ge=0, le=100)
    gross_realized_pnl_usdt: Decimal
    realized_pnl_usdt: Decimal
    commission_usdt: Decimal
    funding_fees_usdt: Decimal
    average_trade_pnl_usdt: Decimal | None = None
    best_trade_pnl_usdt: Decimal | None = None
    worst_trade_pnl_usdt: Decimal | None = None
    pnl_source: JournalPnlSource = JournalPnlSource.VERIFIED_FILLS_NET_ACTUAL_COSTS


class PerformanceDimensionReport(BaseModel):
    """All rows for one stable reporting dimension."""

    dimension: PerformanceDimension
    count: int = Field(ge=0)
    rows: list[PerformanceReportRow]


class VerifiedPerformanceReportResponse(BaseModel):
    """BE-15 report set derived only from verified closed trades."""

    generated_at: datetime
    window_started_at: datetime
    window_ended_at: datetime
    lookback_days: int = Field(ge=1, le=3650)
    source_state: JournalPerformanceState
    verified_closed_trades_only: bool = True
    verified_fill_pnl_only: bool = True
    verified_actual_costs_only: bool = True
    candidate_count: int = Field(ge=0)
    verified_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    rejection_codes: list[str] = Field(default_factory=list)
    strategy: PerformanceDimensionReport
    symbol: PerformanceDimensionReport
    daily: PerformanceDimensionReport
    weekly: PerformanceDimensionReport
    monthly: PerformanceDimensionReport
