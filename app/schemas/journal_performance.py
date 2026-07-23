"""Typed Journal and Performance Engine contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.execution import DemoTradeCloseReason
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.trade_management import TradeManagementState


class JournalPerformanceState(StrEnum):
    """Current readiness of the exchange-verified Journal layer."""

    READY = "READY"
    WAITING_FOR_TRADE_MANAGEMENT = "WAITING_FOR_TRADE_MANAGEMENT"
    EXCHANGE_VERIFICATION_UNAVAILABLE = "EXCHANGE_VERIFICATION_UNAVAILABLE"
    SOURCE_VERIFICATION_INCOMPLETE = "SOURCE_VERIFICATION_INCOMPLETE"


class JournalPnlSource(StrEnum):
    """Authoritative realized-PnL source used by Journal and performance views."""

    VERIFIED_FILLS_GROSS = "VERIFIED_FILLS_GROSS"
    VERIFIED_FILLS_NET_ACTUAL_COSTS = "VERIFIED_FILLS_NET_ACTUAL_COSTS"


class JournalSortBy(StrEnum):
    """Supported journal sorting modes."""

    CLOSED_AT_DESC = "CLOSED_AT_DESC"
    REALIZED_PNL_DESC = "REALIZED_PNL_DESC"
    REALIZED_PNL_ASC = "REALIZED_PNL_ASC"


class JournalEntry(BaseModel):
    """One closed trade admitted only after exchange-source verification."""

    trade_id: str
    signal_id: str
    symbol: str
    direction: ScannerDirection
    setup: ScannerSetup
    setup_name: str
    grade: ScannerGrade | None = None
    entry_price: Decimal
    exit_price: Decimal | None = None
    verified_fill_quantity: Decimal = Field(gt=0)
    realized_pnl_usdt: Decimal = Field(default=Decimal("0"))
    gross_realized_pnl_usdt: Decimal = Field(default=Decimal("0"))
    pnl_source: JournalPnlSource = JournalPnlSource.VERIFIED_FILLS_NET_ACTUAL_COSTS
    tracked_margin_usdt: Decimal = Field(ge=0)
    commission_usdt: Decimal = Field(default=Decimal("0"))
    funding_fees_usdt: Decimal = Field(default=Decimal("0"))
    actual_costs_verified: bool = False
    opened_at: datetime
    closed_at: datetime
    closed_reason: DemoTradeCloseReason
    hold_minutes: int = Field(ge=0)
    source_verified: bool = False
    source_checked_at: datetime | None = None
    entry_exchange_order_id: str | None = None
    close_exchange_order_id: str | None = None
    entry_fill_ids: list[str] = Field(default_factory=list)
    close_fill_ids: list[str] = Field(default_factory=list)
    income_transaction_ids: list[str] = Field(default_factory=list)
    commission_transaction_ids: list[str] = Field(default_factory=list)
    funding_transaction_ids: list[str] = Field(default_factory=list)


class JournalEntryList(BaseModel):
    """Filtered Journal response containing verified-source records only."""

    count: int = Field(ge=0)
    entries: list[JournalEntry]
    verified_source_only: bool = True
    verified_fill_pnl_only: bool = True
    verified_actual_costs_only: bool = True
    source_state: JournalPerformanceState = JournalPerformanceState.READY
    candidate_count: int = Field(default=0, ge=0)
    rejected_count: int = Field(default=0, ge=0)
    rejection_codes: list[str] = Field(default_factory=list)


class JournalPerformanceSummary(BaseModel):
    """Headline summary metrics for dashboard and journal UI."""

    closed_trade_count: int = Field(default=0, ge=0)
    winning_trades: int = Field(default=0, ge=0)
    losing_trades: int = Field(default=0, ge=0)
    breakeven_trades: int = Field(default=0, ge=0)
    realized_pnl_usdt: Decimal = Field(default=Decimal("0"))
    gross_realized_pnl_usdt: Decimal = Field(default=Decimal("0"))
    pnl_source: JournalPnlSource = JournalPnlSource.VERIFIED_FILLS_NET_ACTUAL_COSTS
    commission_usdt: Decimal = Field(default=Decimal("0"))
    funding_fees_usdt: Decimal = Field(default=Decimal("0"))
    win_rate_percent: Decimal = Field(default=Decimal("0"))
    average_win_usdt: Decimal | None = None
    average_loss_usdt: Decimal | None = None
    best_trade_pnl_usdt: Decimal | None = None
    worst_trade_pnl_usdt: Decimal | None = None


class JournalPerformanceStatusResponse(BaseModel):
    """Current verified-source Journal readiness and headline metrics."""

    state: JournalPerformanceState
    contract_version: str = "1"
    journal_performance_implemented: bool = True
    verified_exchange_sources_required: bool = True
    verified_fill_pnl_required: bool = True
    verified_actual_costs_required: bool = True
    trade_management_state: TradeManagementState
    lookback_days: int = Field(default=30, ge=1, le=365)
    latest_closed_trade_at: datetime | None = None
    updated_at: datetime | None = None
    candidate_count: int = Field(default=0, ge=0)
    verified_count: int = Field(default=0, ge=0)
    rejected_count: int = Field(default=0, ge=0)
    rejection_codes: list[str] = Field(default_factory=list)
    summary: JournalPerformanceSummary


class PerformanceSnapshotResponse(BaseModel):
    """Windowed metrics derived only from exchange-verified Journal records."""

    lookback_days: int = Field(ge=1, le=365)
    window_started_at: datetime
    window_ended_at: datetime
    verified_fill_pnl_only: bool = True
    verified_actual_costs_only: bool = True
    source_state: JournalPerformanceState = JournalPerformanceState.READY
    candidate_count: int = Field(default=0, ge=0)
    verified_count: int = Field(default=0, ge=0)
    rejected_count: int = Field(default=0, ge=0)
    rejection_codes: list[str] = Field(default_factory=list)
    summary: JournalPerformanceSummary


class JournalFilters(BaseModel):
    """Normalized closed-trade journal filters."""

    symbol: str | None = None
    direction: ScannerDirection | None = None
    min_grade: ScannerGrade | None = None
    close_reason: DemoTradeCloseReason | None = None
    sort_by: JournalSortBy = JournalSortBy.CLOSED_AT_DESC
