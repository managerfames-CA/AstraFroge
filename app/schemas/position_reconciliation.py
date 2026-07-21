"""Typed contracts for continuous Binance Demo position reconciliation."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.scanner import ScannerDirection


class PositionReconciliationState(StrEnum):
    """Truth state of the latest position reconciliation cycle."""

    NOT_RUN = "NOT_RUN"
    IN_SYNC = "IN_SYNC"
    DRIFT_DETECTED = "DRIFT_DETECTED"
    UNAVAILABLE = "UNAVAILABLE"


class PositionReconciliationFinding(BaseModel):
    """One secret-safe position mismatch."""

    code: str
    message: str
    symbol: str | None = None
    trade_id: str | None = None
    expected_direction: ScannerDirection | None = None
    actual_direction: ScannerDirection | None = None
    expected_quantity: Decimal | None = None
    actual_quantity: Decimal | None = None
    blocking: bool = True


class PositionReconciliationReport(BaseModel):
    """Immutable result of one read-only position reconciliation cycle."""

    state: PositionReconciliationState
    checked_at: datetime
    local_open_trade_count: int = Field(ge=0)
    exchange_open_position_count: int = Field(ge=0)
    blocking: bool
    findings: list[PositionReconciliationFinding] = Field(default_factory=list)
