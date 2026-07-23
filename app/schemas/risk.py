"""Typed account-backed Risk Engine contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.signals import SignalLifecycle


class RiskEngineState(StrEnum):
    """Process-scoped availability of the deterministic risk engine."""

    READY = "READY"
    WAITING_FOR_SIGNALS = "WAITING_FOR_SIGNALS"
    ACCOUNT_UNAVAILABLE = "ACCOUNT_UNAVAILABLE"
    POLICY_LOCKED = "POLICY_LOCKED"


class RiskDecision(StrEnum):
    """Policy result for one signal."""

    APPROVED = "APPROVED"
    WATCH = "WATCH"
    BLOCKED = "BLOCKED"
    TERMINAL = "TERMINAL"


class KillSwitchState(StrEnum):
    """Current kill-switch state."""

    OFFLINE = "OFFLINE"
    ON = "ON"


class RiskRejectionCode(StrEnum):
    """Stable fail-closed Risk Engine rejection and lock codes."""

    DEMO_PRIVATE_API_NOT_CONFIGURED = "DEMO_PRIVATE_API_NOT_CONFIGURED"
    DEMO_PRIVATE_API_UNAVAILABLE = "DEMO_PRIVATE_API_UNAVAILABLE"
    PRIVATE_ACCOUNT_PAYLOAD_INVALID = "PRIVATE_ACCOUNT_PAYLOAD_INVALID"
    ACCOUNT_CANNOT_TRADE = "ACCOUNT_CANNOT_TRADE"
    ACCOUNT_BALANCE_UNAVAILABLE = "ACCOUNT_BALANCE_UNAVAILABLE"
    RISK_PERCENT_NOT_CONFIGURED = "RISK_PERCENT_NOT_CONFIGURED"
    STOP_LOSS_MISSING = "STOP_LOSS_MISSING"
    STOP_LOSS_INVALID = "STOP_LOSS_INVALID"
    GRADE_NOT_EXECUTABLE = "GRADE_NOT_EXECUTABLE"
    MAX_OPEN_TRADES_REACHED = "MAX_OPEN_TRADES_REACHED"
    SAME_SYMBOL_POSITION_EXISTS = "SAME_SYMBOL_POSITION_EXISTS"
    CONFLICTING_SYMBOL_POSITION_EXISTS = "CONFLICTING_SYMBOL_POSITION_EXISTS"
    SYMBOL_LEVERAGE_UNAVAILABLE = "SYMBOL_LEVERAGE_UNAVAILABLE"
    MAX_MARGIN_EXPOSURE_NOT_CONFIGURED = "MAX_MARGIN_EXPOSURE_NOT_CONFIGURED"
    MAX_MARGIN_EXPOSURE_REACHED = "MAX_MARGIN_EXPOSURE_REACHED"
    AVAILABLE_BALANCE_INSUFFICIENT = "AVAILABLE_BALANCE_INSUFFICIENT"
    DAILY_PNL_BASELINE_UNAVAILABLE = "DAILY_PNL_BASELINE_UNAVAILABLE"
    DAILY_LOSS_LIMIT_REACHED = "DAILY_LOSS_LIMIT_REACHED"
    DAILY_PROFIT_LOCK_REACHED = "DAILY_PROFIT_LOCK_REACHED"


class RiskAssessment(BaseModel):
    """One signal evaluated against deterministic account-backed risk policy."""

    signal_id: str
    symbol: str
    direction: ScannerDirection
    setup: ScannerSetup
    setup_name: str
    signal_lifecycle: SignalLifecycle
    grade: ScannerGrade | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    confidence: int | None = Field(default=None, ge=0, le=100)
    decision: RiskDecision
    blocked_reason: str | None = None
    approved_for_execution: bool
    entry_trigger_price: Decimal
    stop_loss_price: Decimal | None = None
    stop_distance: Decimal | None = Field(default=None, gt=0)
    risk_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    risk_budget_usdt: Decimal | None = Field(default=None, ge=0)
    recommended_quantity: Decimal | None = Field(default=None, gt=0)
    position_notional_usdt: Decimal | None = Field(default=None, ge=0)
    required_margin_usdt: Decimal | None = Field(default=None, ge=0)
    wallet_balance_usdt: Decimal | None = Field(default=None, ge=0)
    available_balance_usdt: Decimal | None = Field(default=None, ge=0)
    daily_realized_pnl_usdt: Decimal | None = None
    daily_unrealized_pnl_usdt: Decimal | None = None
    daily_net_pnl_usdt: Decimal | None = None
    daily_pnl_percent: Decimal | None = None
    open_position_count: int = Field(default=0, ge=0)
    current_margin_exposure_usdt: Decimal = Field(ge=0)
    max_open_trades_limit: int = Field(ge=1)
    updated_at: datetime
    audit_codes: list[str] = Field(default_factory=list)


class RiskSummary(BaseModel):
    """Frontend-friendly counts for the risk dashboard."""

    approved: int = Field(default=0, ge=0)
    blocked: int = Field(default=0, ge=0)
    watch: int = Field(default=0, ge=0)
    terminal: int = Field(default=0, ge=0)


class RiskStatusResponse(BaseModel):
    """Current deterministic risk-engine state and verified account snapshot."""

    state: RiskEngineState
    contract_version: str = "1"
    risk_engine_implemented: bool = True
    signal_engine_required: bool = True
    signal_engine_state: str
    account_snapshot_available: bool = False
    account_can_trade: bool = False
    wallet_balance_usdt: Decimal | None = Field(default=None, ge=0)
    available_balance_usdt: Decimal | None = Field(default=None, ge=0)
    daily_realized_pnl_usdt: Decimal | None = None
    daily_unrealized_pnl_usdt: Decimal | None = None
    daily_net_pnl_usdt: Decimal | None = None
    daily_pnl_percent: Decimal | None = None
    risk_per_trade_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    daily_loss_limit_percent: Decimal = Field(ge=0, le=100)
    daily_profit_lock_percent: Decimal = Field(ge=0, le=100)
    current_margin_exposure_usdt: Decimal = Field(ge=0)
    max_margin_exposure_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    open_position_count: int = Field(default=0, ge=0)
    max_open_trades_limit: int = Field(ge=1)
    available_tracking_slots: int = Field(ge=0)
    emergency_kill_switch: KillSwitchState
    lock_reason: str | None = None
    updated_at: datetime | None = None
    summary: RiskSummary


class RiskAssessmentList(BaseModel):
    """Risk assessments derived from one verified account snapshot."""

    count: int = Field(ge=0)
    assessments: list[RiskAssessment]
