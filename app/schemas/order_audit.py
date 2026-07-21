"""Typed exchange-authoritative order audit contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class OrderAuditState(StrEnum):
    """Current durable order-audit verification state."""

    NOT_RUN = "NOT_RUN"
    READY = "READY"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"


class OrderAuditRole(StrEnum):
    """Stable functional role of one Binance Demo order."""

    ENTRY = "ENTRY"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MANUAL_CLOSE = "MANUAL_CLOSE"


class OrderAuditFinding(BaseModel):
    """Secret-safe reason why one order audit could not be proved."""

    code: str
    message: str
    trade_id: str | None = None
    symbol: str | None = None
    client_order_id: str | None = None


class OrderAuditRecord(BaseModel):
    """One durable exchange-authoritative order audit record."""

    order_id: str
    signal_id: str | None = None
    trade_id: str | None = None
    role: OrderAuditRole
    symbol: str
    client_order_id: str
    exchange_order_id: str
    actual_order_id: str | None = None
    requested_quantity: Decimal = Field(gt=0)
    executed_quantity: Decimal = Field(ge=0)
    average_fill_price: Decimal | None = Field(default=None, gt=0)
    final_status: str
    exchange_trade_ids: list[str] = Field(default_factory=list)
    source: str
    verified_at: datetime
    created_at: datetime
    updated_at: datetime


class OrderAuditRecordList(BaseModel):
    """Collection of durable order audit records."""

    count: int = Field(ge=0)
    records: list[OrderAuditRecord]
    state: OrderAuditState
    blocking: bool
    findings: list[OrderAuditFinding] = Field(default_factory=list)


class OrderAuditStatusResponse(BaseModel):
    """Summary of the latest order-audit reconciliation."""

    state: OrderAuditState
    checked_at: datetime
    tracked_trade_count: int = Field(ge=0)
    audited_order_count: int = Field(ge=0)
    entry_order_count: int = Field(ge=0)
    protective_order_count: int = Field(ge=0)
    manual_close_order_count: int = Field(ge=0)
    blocking: bool
    findings: list[OrderAuditFinding] = Field(default_factory=list)
