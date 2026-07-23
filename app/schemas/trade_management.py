"""Typed Trade Management Engine contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.execution import DemoTradeRecord
from app.schemas.scanner import ScannerDirection, ScannerGrade


class TradeManagementState(StrEnum):
    """Current reconciliation readiness of the trade-management layer."""

    READY = "READY"
    WAITING_FOR_EXECUTION = "WAITING_FOR_EXECUTION"
    EXCHANGE_VERIFICATION_UNAVAILABLE = "EXCHANGE_VERIFICATION_UNAVAILABLE"
    SOURCE_VERIFICATION_INCOMPLETE = "SOURCE_VERIFICATION_INCOMPLETE"


class ManualCloseIntentState(StrEnum):
    """Durable lifecycle of one deterministic manual-close operation."""

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    COMPLETED = "COMPLETED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class ManualCloseIntentSnapshot(BaseModel):
    """Typed durable close state reused across retries, restarts and instances."""

    intent_id: str
    trade_id: str
    client_order_id: str
    state: ManualCloseIntentState
    exchange_order_id: str | None = None
    order_status: str | None = None
    completed_trade: DemoTradeRecord | None = None
    updated_at: datetime


class TradeSortBy(StrEnum):
    """Supported active-trades sort modes."""

    OPENED_AT_DESC = "OPENED_AT_DESC"
    UNREALIZED_PNL_DESC = "UNREALIZED_PNL_DESC"
    UNREALIZED_PNL_ASC = "UNREALIZED_PNL_ASC"


class TradeManagementSummary(BaseModel):
    """Frontend-friendly counts for the Active Trades page."""

    manual_demo_trades: int = Field(default=0, ge=0)
    long_demo: int = Field(default=0, ge=0)
    short_demo: int = Field(default=0, ge=0)
    combined_unrealized_pnl_usdt: Decimal = Field(default=Decimal("0"))
    total_tracked_margin_usdt: Decimal = Field(default=Decimal("0"))


class TradeManagementStatusResponse(BaseModel):
    """Current trade-management readiness and open-trade summary."""

    state: TradeManagementState
    contract_version: str = "1"
    trade_management_implemented: bool = True
    exchange_authoritative_active_trades: bool = False
    execution_engine_state: str
    max_open_trades_limit: int = Field(ge=1)
    tracked_trade_count: int = Field(ge=0)
    open_trade_count: int = Field(ge=0)
    available_tracking_slots: int = Field(ge=0)
    local_open_candidate_count: int = Field(default=0, ge=0)
    exchange_open_position_count: int = Field(default=0, ge=0)
    rejected_open_trade_count: int = Field(default=0, ge=0)
    rejection_codes: list[str] = Field(default_factory=list)
    position_snapshot_id: str | None = None
    position_snapshot_at: datetime | None = None
    updated_at: datetime | None = None
    summary: TradeManagementSummary


class ManagedTradeRecordList(BaseModel):
    """Filtered tracked trade response."""

    count: int = Field(ge=0)
    trades: list[DemoTradeRecord]
    source_state: TradeManagementState = TradeManagementState.READY
    exchange_authoritative_open_trades: bool = False
    local_open_candidate_count: int = Field(default=0, ge=0)
    exchange_open_position_count: int = Field(default=0, ge=0)
    rejected_open_trade_count: int = Field(default=0, ge=0)
    rejection_codes: list[str] = Field(default_factory=list)
    position_snapshot_id: str | None = None
    position_snapshot_at: datetime | None = None


class TradeCloseReason(StrEnum):
    """Allowed operator close reasons for tracked Demo trades."""

    MANUAL_CLOSE = "MANUAL_CLOSE"
    INVALIDATED = "INVALIDATED"


class TradeCloseRequest(BaseModel):
    """Operator close intent; exchange price and PnL are never client supplied."""

    reason: TradeCloseReason = TradeCloseReason.MANUAL_CLOSE
    exit_price: Decimal | None = Field(default=None, deprecated=True)
    realized_pnl_usdt: Decimal | None = Field(default=None, deprecated=True)


class TradeListFilters(BaseModel):
    """Normalized filter set for the trade-management list operation."""

    symbol: str | None = None
    direction: ScannerDirection | None = None
    min_grade: ScannerGrade | None = None
    include_closed: bool = False
    sort_by: TradeSortBy = TradeSortBy.OPENED_AT_DESC
