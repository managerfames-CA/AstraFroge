"""Typed contracts for exchange-verified protective lifecycle events."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.execution import DemoTradeCloseReason


class ProtectiveLifecycleState(StrEnum):
    """Truth state of the protective lifecycle verification surface."""

    NOT_RUN = "NOT_RUN"
    IN_SYNC = "IN_SYNC"
    PARTIAL_CLOSE_VERIFIED = "PARTIAL_CLOSE_VERIFIED"
    CLOSED_VERIFIED = "CLOSED_VERIFIED"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"


class ProtectiveLifecycleEventType(StrEnum):
    """Durable lifecycle event types derived from exchange fills."""

    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"


class ProtectiveLifecycleEvent(BaseModel):
    """One idempotent exchange-authoritative protective fill event."""

    event_id: str
    event_type: ProtectiveLifecycleEventType
    trade_id: str
    signal_id: str
    symbol: str
    close_reason: DemoTradeCloseReason
    client_order_id: str
    algo_order_id: str
    actual_order_id: str
    exchange_trade_id: str
    fill_quantity: Decimal = Field(gt=0)
    fill_price: Decimal = Field(gt=0)
    cumulative_exit_quantity: Decimal = Field(gt=0)
    remaining_quantity: Decimal = Field(ge=0)
    filled_at: datetime
    recorded_at: datetime


class ProtectiveLifecycleFinding(BaseModel):
    """One secret-safe failure to prove a protective lifecycle transition."""

    code: str
    message: str
    trade_id: str | None = None
    symbol: str | None = None
    blocking: bool = True


class ProtectiveLifecycleReport(BaseModel):
    """Result of one exchange-authoritative lifecycle verification cycle."""

    state: ProtectiveLifecycleState
    checked_at: datetime
    open_trade_count: int = Field(ge=0)
    verified_event_count: int = Field(ge=0)
    partial_trade_count: int = Field(ge=0)
    closed_trade_count: int = Field(ge=0)
    blocking: bool
    findings: list[ProtectiveLifecycleFinding] = Field(default_factory=list)
    events: list[ProtectiveLifecycleEvent] = Field(default_factory=list)
