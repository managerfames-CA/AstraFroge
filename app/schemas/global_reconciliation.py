"""Typed global reconciliation fail-closed contract."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.lifecycle_reconciliation import LifecycleReconciliationState
from app.schemas.order_reconciliation import OrderReconciliationState
from app.schemas.position_reconciliation import PositionReconciliationState
from app.schemas.restart_recovery import RestartRecoveryState


class GlobalReconciliationState(StrEnum):
    """Authoritative combined safety state for automated Demo execution."""

    NOT_RUN = "NOT_RUN"
    SAFE = "SAFE"
    BLOCKED = "BLOCKED"
    UNAVAILABLE = "UNAVAILABLE"


class GlobalReconciliationReport(BaseModel):
    """One secret-safe proof covering every exchange-truth surface."""

    state: GlobalReconciliationState
    checked_at: datetime
    order_state: OrderReconciliationState
    position_state: PositionReconciliationState
    lifecycle_state: LifecycleReconciliationState
    restart_state: RestartRecoveryState
    automation_ready: bool
    blocking: bool
    error_count: int = Field(ge=0)
    error_codes: list[str] = Field(default_factory=list)
