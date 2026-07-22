"""Normalize continuous order and position reconciliation into BE-05 findings."""

from __future__ import annotations

from typing import Protocol

from app.schemas.lifecycle_reconciliation import (
    LifecycleMismatchCategory,
    LifecycleMismatchFinding,
    LifecycleReconciliationReport,
    LifecycleReconciliationState,
)
from app.schemas.order_reconciliation import (
    OrderReconciliationFinding,
    OrderReconciliationReport,
    OrderReconciliationState,
)
from app.schemas.position_reconciliation import (
    PositionReconciliationFinding,
    PositionReconciliationReport,
    PositionReconciliationState,
)

_ORDER_READY_STATES = frozenset(
    {OrderReconciliationState.IN_SYNC, OrderReconciliationState.DRIFT_DETECTED}
)
_POSITION_READY_STATES = frozenset(
    {PositionReconciliationState.IN_SYNC, PositionReconciliationState.DRIFT_DETECTED}
)


class OrderReconciliationSource(Protocol):
    def latest(self) -> OrderReconciliationReport: ...


class PositionReconciliationSource(Protocol):
    def latest(self) -> PositionReconciliationReport: ...


class LifecycleMismatchDetectionService:
    """Build one truthful lifecycle view from continuously refreshed sources."""

    def __init__(
        self,
        order_source: OrderReconciliationSource,
        position_source: PositionReconciliationSource,
    ) -> None:
        self._order_source = order_source
        self._position_source = position_source

    def latest(self) -> LifecycleReconciliationReport:
        """Return the latest combined classification without exchange mutations."""

        order_report = self._order_source.latest()
        position_report = self._position_source.latest()
        findings = [self._from_order(item) for item in order_report.findings if item.blocking]
        findings.extend(
            self._from_position(item) for item in position_report.findings if item.blocking
        )

        sources_ready = (
            order_report.state in _ORDER_READY_STATES
            and position_report.state in _POSITION_READY_STATES
        )
        if not sources_ready:
            if order_report.state not in _ORDER_READY_STATES:
                findings.append(
                    LifecycleMismatchFinding(
                        category=LifecycleMismatchCategory.EXCHANGE_RUNTIME_MISMATCH,
                        code="ORDER_RECONCILIATION_NOT_READY",
                        message="Continuous order reconciliation has not proved exchange truth",
                        source="order-reconciliation",
                    )
                )
            if position_report.state not in _POSITION_READY_STATES:
                findings.append(
                    LifecycleMismatchFinding(
                        category=LifecycleMismatchCategory.EXCHANGE_RUNTIME_MISMATCH,
                        code="POSITION_RECONCILIATION_NOT_READY",
                        message="Continuous position reconciliation has not proved exchange truth",
                        source="position-reconciliation",
                    )
                )

        blocking = bool(findings)
        if not sources_ready:
            state = LifecycleReconciliationState.UNAVAILABLE
        elif blocking:
            state = LifecycleReconciliationState.MISMATCH_DETECTED
        else:
            state = LifecycleReconciliationState.IN_SYNC

        return LifecycleReconciliationReport(
            state=state,
            checked_at=max(order_report.checked_at, position_report.checked_at),
            order_state=order_report.state,
            position_state=position_report.state,
            blocking=blocking,
            finding_count=len(findings),
            findings=findings,
        )

    @staticmethod
    def _from_order(item: OrderReconciliationFinding) -> LifecycleMismatchFinding:
        if item.code in {
            "ENTRY_ORDER_PARTIAL_FILL",
            "PROTECTIVE_ORDER_PARTIAL_FILL",
        }:
            category = LifecycleMismatchCategory.PARTIAL_FILL
        elif item.code == "PROTECTIVE_ORDER_MISSING":
            category = LifecycleMismatchCategory.MISSING_PROTECTION
        else:
            category = LifecycleMismatchCategory.EXCHANGE_RUNTIME_MISMATCH
        return LifecycleMismatchFinding(
            category=category,
            code=item.code,
            message=item.message,
            source="order-reconciliation",
            symbol=item.symbol,
            trade_id=item.trade_id,
            client_order_id=item.client_order_id,
            blocking=item.blocking,
        )

    @staticmethod
    def _from_position(
        item: PositionReconciliationFinding,
    ) -> LifecycleMismatchFinding:
        category = (
            LifecycleMismatchCategory.EXTERNAL_CLOSE
            if item.code == "EXTERNAL_POSITION_CLOSE_DETECTED"
            else LifecycleMismatchCategory.EXCHANGE_RUNTIME_MISMATCH
        )
        return LifecycleMismatchFinding(
            category=category,
            code=item.code,
            message=item.message,
            source="position-reconciliation",
            symbol=item.symbol,
            trade_id=item.trade_id,
            blocking=item.blocking,
        )
