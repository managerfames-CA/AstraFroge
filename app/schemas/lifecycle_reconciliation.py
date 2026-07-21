"""Typed contracts for BE-05 exchange/runtime lifecycle mismatch detection."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.order_reconciliation import OrderReconciliationState
from app.schemas.position_reconciliation import PositionReconciliationState


class LifecycleMismatchCategory(StrEnum):
    """Normalized categories required by the BE-05 audit boundary."""

    PARTIAL_FILL = "PARTIAL_FILL"
    EXTERNAL_CLOSE = "EXTERNAL_CLOSE"
    MISSING_PROTECTION = "MISSING_PROTECTION"
    EXCHANGE_RUNTIME_MISMATCH = "EXCHANGE_RUNTIME_MISMATCH"


class LifecycleReconciliationState(StrEnum):
    """Truth state of the combined order and position lifecycle view."""

    IN_SYNC = "IN_SYNC"
    MISMATCH_DETECTED = "MISMATCH_DETECTED"
    UNAVAILABLE = "UNAVAILABLE"


class LifecycleMismatchFinding(BaseModel):
    """One normalized, secret-safe lifecycle mismatch."""

    category: LifecycleMismatchCategory
    code: str
    message: str
    source: str
    symbol: str | None = None
    trade_id: str | None = None
    client_order_id: str | None = None
    blocking: bool = True


class LifecycleReconciliationReport(BaseModel):
    """Combined continuously refreshed order/position reconciliation status."""

    state: LifecycleReconciliationState
    checked_at: datetime
    order_state: OrderReconciliationState
    position_state: PositionReconciliationState
    blocking: bool
    finding_count: int = Field(ge=0)
    findings: list[LifecycleMismatchFinding] = Field(default_factory=list)
