"""Typed contracts for BE-06 restart and deployment recovery ownership."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.recovery import RecoveryState


class RestartRecoveryState(StrEnum):
    """Outcome of rebuilding runtime ownership after process replacement."""

    NOT_READY = "NOT_READY"
    RECOVERED = "RECOVERED"
    BLOCKED = "BLOCKED"


class RestartRecoveryReport(BaseModel):
    """Secret-safe proof that durable open state matches Binance Demo truth."""

    state: RestartRecoveryState
    checked_at: datetime
    recovery_state: RecoveryState
    exchange_reconciled: bool
    automation_ready: bool
    recovered_open_trade_count: int = Field(ge=0)
    recovered_open_order_count: int = Field(ge=0)
    recovered_open_position_count: int = Field(ge=0)
    recovered_trade_ids: list[str] = Field(default_factory=list)
    recovered_order_client_ids: list[str] = Field(default_factory=list)
    recovered_position_symbols: list[str] = Field(default_factory=list)
    blocking: bool
    error: str | None = None
