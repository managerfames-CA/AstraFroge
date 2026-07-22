"""Typed Demo Execution Engine contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.recovery import RecoveryState
from app.schemas.risk import RiskDecision
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.signals import SignalLifecycle


class DemoExecutionState(StrEnum):
    """Current orchestration state of the demo execution engine."""

    READY = "READY"
    WAITING_FOR_RISK = "WAITING_FOR_RISK"
    EXECUTION_LOCKED = "EXECUTION_LOCKED"


class DemoPlanState(StrEnum):
    """Execution readiness of one signal-derived demo plan."""

    EXECUTABLE = "EXECUTABLE"
    WATCH = "WATCH"
    BLOCKED = "BLOCKED"
    TERMINAL = "TERMINAL"


class DemoTradeLifecycle(StrEnum):
    """Tracked demo trade lifecycle."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


class DemoProtectionState(StrEnum):
    """Exchange-confirmed protective-order state for one open trade."""

    PROTECTED = "PROTECTED"


class DemoTradeCloseReason(StrEnum):
    """Recorded close reason for one tracked demo trade."""

    MANUAL_CLOSE = "MANUAL_CLOSE"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    INVALIDATED = "INVALIDATED"


class DemoOrderSide(StrEnum):
    """Signed Binance demo order side."""

    BUY = "BUY"
    SELL = "SELL"


class DemoExecutionPlan(BaseModel):
    """One deterministic demo execution plan derived from a risk assessment."""

    signal_id: str
    symbol: str
    direction: ScannerDirection
    setup: ScannerSetup
    setup_name: str
    signal_lifecycle: SignalLifecycle
    risk_decision: RiskDecision
    plan_state: DemoPlanState
    grade: ScannerGrade | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    confidence: int | None = Field(default=None, ge=0, le=100)
    entry_trigger_price: Decimal
    stop_loss_price: Decimal | None = Field(default=None, gt=0)
    recommended_quantity: Decimal | None = Field(default=None, gt=0)
    take_profit_r_multiple: Decimal = Field(default=Decimal("0"), ge=0, le=20)
    blocked_reason: str | None = None
    executable_now: bool
    updated_at: datetime
    audit_codes: list[str] = Field(default_factory=list)


class DemoTradeRecord(BaseModel):
    """One exchange-confirmed protected Binance Demo trade."""

    trade_id: str
    signal_id: str
    symbol: str
    direction: ScannerDirection
    setup: ScannerSetup
    setup_name: str
    lifecycle: DemoTradeLifecycle
    protection_state: DemoProtectionState
    grade: ScannerGrade | None = None
    entry_price: Decimal = Field(gt=0)
    stop_loss_price: Decimal = Field(gt=0)
    take_profit_price: Decimal = Field(gt=0)
    exit_price: Decimal | None = None
    exchange_order_id: str
    client_order_id: str
    stop_order_id: str
    stop_client_order_id: str
    take_profit_order_id: str
    take_profit_client_order_id: str
    requested_quantity: Decimal = Field(gt=0)
    executed_quantity: Decimal = Field(gt=0)
    remaining_quantity: Decimal | None = Field(default=None, ge=0)
    protective_exit_filled_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    protective_exit_notional_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    protective_exit_fill_ids: list[str] = Field(default_factory=list)
    protective_exit_order_ids: list[str] = Field(default_factory=list)
    protective_exit_reason: DemoTradeCloseReason | None = None
    partial_close_count: int = Field(default=0, ge=0)
    last_lifecycle_event_id: str | None = None
    last_lifecycle_event_at: datetime | None = None
    protective_sibling_cancelled: bool | None = None
    order_status: str
    tracked_margin_usdt: Decimal = Field(ge=0)
    unrealized_pnl_usdt: Decimal = Field(default=Decimal("0"))
    realized_pnl_usdt: Decimal = Field(default=Decimal("0"))
    gross_realized_pnl_usdt: Decimal = Field(default=Decimal("0"))
    commission_usdt: Decimal = Field(default=Decimal("0"))
    funding_fees_usdt: Decimal = Field(default=Decimal("0"))
    exchange_position_verified: bool = False
    position_snapshot_id: str | None = None
    position_snapshot_at: datetime | None = None
    position_source: str | None = None
    exchange_position_quantity: Decimal | None = Field(default=None, gt=0)
    opened_at: datetime
    closed_at: datetime | None = None
    closed_reason: DemoTradeCloseReason | None = None
    updated_at: datetime

    @property
    def effective_open_quantity(self) -> Decimal:
        """Return the latest verified remaining position quantity."""

        return (
            self.remaining_quantity
            if self.remaining_quantity is not None
            else self.executed_quantity
        )


class DemoExecutionSummary(BaseModel):
    """Frontend-friendly summary counts for the active trades page."""

    executable_plans: int = Field(default=0, ge=0)
    blocked_plans: int = Field(default=0, ge=0)
    watch_plans: int = Field(default=0, ge=0)
    open_trades: int = Field(default=0, ge=0)
    long_demo: int = Field(default=0, ge=0)
    short_demo: int = Field(default=0, ge=0)


class DemoExecutionStatusResponse(BaseModel):
    """Current demo execution status and truthful lock state."""

    state: DemoExecutionState
    demo_execution_implemented: bool = True
    execution_enabled: bool
    demo_credentials_configured: bool
    private_api_available: bool
    risk_engine_state: str
    take_profit_r_multiple: Decimal = Field(ge=0, le=20)
    max_open_trades_limit: int = Field(ge=1)
    tracked_trade_count: int = Field(ge=0)
    available_tracking_slots: int = Field(ge=0)
    combined_unrealized_pnl_usdt: Decimal = Field(default=Decimal("0"))
    total_tracked_margin_usdt: Decimal = Field(default=Decimal("0"))
    recovery_state: RecoveryState = RecoveryState.RECOVERY_REQUIRED
    exchange_reconciled: bool = False
    signals_revalidated: bool = False
    automation_ready: bool = False
    last_recovery_at: datetime | None = None
    recovery_error: str | None = None
    execution_integration_ready: bool = False
    execution_unavailable_reason: str | None = None
    updated_at: datetime | None = None
    summary: DemoExecutionSummary


class DemoExecutionPlanList(BaseModel):
    """Filtered demo execution plans."""

    count: int = Field(ge=0)
    plans: list[DemoExecutionPlan]


class DemoTradeRecordList(BaseModel):
    """Filtered tracked demo trades."""

    count: int = Field(ge=0)
    trades: list[DemoTradeRecord]


class DemoExecutionActivateRequest(BaseModel):
    """Deprecated client quantity field retained only for explicit rejection."""

    quantity: Decimal | None = Field(default=None, gt=0, deprecated=True)


class DemoAccountBalance(BaseModel):
    """One asset balance from the Binance demo account."""

    asset: str
    wallet_balance: Decimal
    available_balance: Decimal
    unrealized_pnl: Decimal


class DemoPositionSnapshot(BaseModel):
    """One open demo position snapshot."""

    symbol: str
    side: ScannerDirection
    quantity: Decimal = Field(ge=0)
    entry_price: Decimal = Field(ge=0)
    unrealized_pnl: Decimal


class DemoExecutionAccountResponse(BaseModel):
    """Current connected demo account snapshot."""

    demo_private_execution_ready: bool
    can_trade: bool
    updated_at: datetime
    total_wallet_balance_usdt: Decimal
    available_balance_usdt: Decimal
    total_unrealized_pnl_usdt: Decimal
    balances: list[DemoAccountBalance]
    open_positions: list[DemoPositionSnapshot]


class DemoAccountDiagnosticResponse(BaseModel):
    """Secret-safe Demo account connectivity diagnostic."""

    diagnostic_status: str
    demo_base_url_configured: bool
    demo_base_url_host: str | None = None
    demo_api_key_configured: bool
    demo_api_secret_configured: bool
    demo_credentials_configured: bool
    private_client_available: bool
    execution_enabled: bool
    take_profit_r_multiple: Decimal = Field(ge=0, le=20)
    account_endpoint_status: str
    account_can_trade: bool | None = None
    account_error_code: str | None = None
    account_error_message: str | None = None
    account_error_status_code: int | None = Field(default=None, ge=100, le=599)
    account_exchange_code: int | None = None
    checked_at: datetime
