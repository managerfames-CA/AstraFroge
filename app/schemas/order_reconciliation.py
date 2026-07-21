"""Typed contracts for continuous Binance Demo order reconciliation."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class OrderReconciliationState(StrEnum):
    NOT_RUN = "NOT_RUN"
    IN_SYNC = "IN_SYNC"
    DRIFT_DETECTED = "DRIFT_DETECTED"
    UNAVAILABLE = "UNAVAILABLE"


class OrderReconciliationFinding(BaseModel):
    code: str
    message: str
    symbol: str | None = None
    trade_id: str | None = None
    client_order_id: str | None = None
    blocking: bool = True


class OrderReconciliationReport(BaseModel):
    state: OrderReconciliationState
    checked_at: datetime
    local_open_trade_count: int = Field(ge=0)
    exchange_open_regular_order_count: int = Field(ge=0)
    exchange_open_algo_order_count: int = Field(ge=0)
    blocking: bool
    findings: list[OrderReconciliationFinding] = Field(default_factory=list)
