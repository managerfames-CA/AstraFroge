"""Typed Phase 5 execution-command and worker observability contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup


class ExecutionCommandState(StrEnum):
    """Durable, fail-closed lifecycle for one approved new-entry request."""

    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    SUBMITTING = "SUBMITTING"
    ENTRY_CONFIRMED = "ENTRY_CONFIRMED"
    PROTECTION_PENDING = "PROTECTION_PENDING"
    PROTECTED = "PROTECTED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    EXPIRED = "EXPIRED"


class ExecutionCommand(BaseModel):
    """Immutable approved execution request persisted before any exchange mutation."""

    model_config = ConfigDict(frozen=True)

    command_id: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=64, max_length=64)
    deterministic_execution_identity: str = Field(min_length=64, max_length=64)
    signal_id: str = Field(min_length=64, max_length=64)
    decision_key: str = Field(min_length=64, max_length=64)
    risk_decision_id: str = Field(min_length=64, max_length=64)
    symbol: str = Field(min_length=1, max_length=32)
    direction: ScannerDirection
    setup: ScannerSetup
    grade: ScannerGrade
    source_snapshot_version: str = Field(min_length=1, max_length=256)
    entry_trigger_price: Decimal = Field(gt=0)
    stop_loss_price: Decimal = Field(gt=0)
    take_profit_price: Decimal = Field(gt=0)
    take_profit_r_multiple: Decimal = Field(gt=0)
    approved_quantity: Decimal = Field(gt=0)
    approved_notional: Decimal = Field(gt=0)
    approved_margin: Decimal = Field(gt=0)
    leverage: int = Field(ge=1)
    account_snapshot_id: str = Field(min_length=64, max_length=64)
    state: ExecutionCommandState
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    entry_client_order_id: str = Field(min_length=1, max_length=128)
    stop_client_order_id: str = Field(min_length=1, max_length=128)
    take_profit_client_order_id: str = Field(min_length=1, max_length=128)
    entry_exchange_order_id: str | None = None
    stop_exchange_order_id: str | None = None
    take_profit_exchange_order_id: str | None = None
    executed_quantity: Decimal | None = Field(default=None, gt=0)
    average_entry_price: Decimal | None = Field(default=None, gt=0)
    claim_token: str | None = None
    worker_id: str | None = None
    claimed_at: datetime | None = None
    failure_reason: str | None = None
    audit_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_command(self) -> ExecutionCommand:
        if self.grade not in {ScannerGrade.A_PLUS, ScannerGrade.A}:
            raise ValueError("Only A+ or A decisions can form execution commands")
        if self.direction is ScannerDirection.LONG:
            if self.stop_loss_price >= self.entry_trigger_price:
                raise ValueError("LONG stop loss must be below the entry trigger")
            if self.take_profit_price <= self.entry_trigger_price:
                raise ValueError("LONG take profit must be above the entry trigger")
        else:
            if self.stop_loss_price <= self.entry_trigger_price:
                raise ValueError("SHORT stop loss must be above the entry trigger")
            if self.take_profit_price >= self.entry_trigger_price:
                raise ValueError("SHORT take profit must be below the entry trigger")
        if self.expires_at <= self.created_at:
            raise ValueError("Execution command expiry must be after creation")
        return self


class ExecutionCommandTransition(BaseModel):
    """One immutable command state transition."""

    sequence: int = Field(ge=1)
    from_state: ExecutionCommandState | None = None
    to_state: ExecutionCommandState
    reason: str = Field(min_length=1, max_length=128)
    changed_at: datetime


class ExecutionCommandList(BaseModel):
    count: int = Field(ge=0)
    commands: list[ExecutionCommand]


class ExecutionCommandStatus(BaseModel):
    execution_enabled: bool
    persistence_available: bool
    pending: int = Field(ge=0)
    claimed: int = Field(ge=0)
    recovery_required: int = Field(ge=0)
    completed: int = Field(ge=0)
    blocked: int = Field(ge=0)
    failed: int = Field(ge=0)
    expired: int = Field(ge=0)
    updated_at: datetime | None = None


class ExecutionWorkerStatus(BaseModel):
    worker_id: str
    execution_enabled: bool
    recovery_ready: bool
    leader_valid: bool
    persistence_available: bool
    last_cycle_at: datetime | None = None
    last_command_id: str | None = None
    last_result: str | None = None
