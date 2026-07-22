"""Focused BE-05 lifecycle mismatch classification tests."""

from __future__ import annotations

from datetime import UTC, datetime

from app.schemas.lifecycle_reconciliation import (
    LifecycleMismatchCategory,
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
from app.services.lifecycle_reconciliation import LifecycleMismatchDetectionService

NOW = datetime(2026, 7, 19, 13, 40, tzinfo=UTC)


class _OrderSource:
    def __init__(self, report: OrderReconciliationReport) -> None:
        self._report = report

    def latest(self) -> OrderReconciliationReport:
        return self._report


class _PositionSource:
    def __init__(self, report: PositionReconciliationReport) -> None:
        self._report = report

    def latest(self) -> PositionReconciliationReport:
        return self._report


def _order_report(
    state: OrderReconciliationState = OrderReconciliationState.IN_SYNC,
    findings: list[OrderReconciliationFinding] | None = None,
) -> OrderReconciliationReport:
    rows = findings or []
    return OrderReconciliationReport(
        state=state,
        checked_at=NOW,
        local_open_trade_count=1,
        exchange_open_regular_order_count=0,
        exchange_open_algo_order_count=2,
        blocking=bool(rows),
        findings=rows,
    )


def _position_report(
    state: PositionReconciliationState = PositionReconciliationState.IN_SYNC,
    findings: list[PositionReconciliationFinding] | None = None,
) -> PositionReconciliationReport:
    rows = findings or []
    return PositionReconciliationReport(
        state=state,
        checked_at=NOW,
        local_open_trade_count=1,
        exchange_open_position_count=1,
        blocking=bool(rows),
        findings=rows,
    )


def test_lifecycle_reconciliation_reports_in_sync_sources() -> None:
    report = LifecycleMismatchDetectionService(
        _OrderSource(_order_report()),
        _PositionSource(_position_report()),
    ).latest()

    assert report.state is LifecycleReconciliationState.IN_SYNC
    assert report.blocking is False
    assert report.finding_count == 0
    assert report.findings == []


def test_lifecycle_reconciliation_classifies_required_be05_mismatches() -> None:
    order_findings = [
        OrderReconciliationFinding(
            code="ENTRY_ORDER_PARTIAL_FILL",
            message="partial entry",
            symbol="BTCUSDT",
            trade_id="trade-1",
        ),
        OrderReconciliationFinding(
            code="PROTECTIVE_ORDER_MISSING",
            message="missing stop",
            symbol="BTCUSDT",
            trade_id="trade-1",
            client_order_id="stop-1",
        ),
        OrderReconciliationFinding(
            code="ENTRY_FILL_QUANTITY_MISMATCH",
            message="quantity mismatch",
            symbol="ETHUSDT",
            trade_id="trade-2",
        ),
    ]
    position_findings = [
        PositionReconciliationFinding(
            code="EXTERNAL_POSITION_CLOSE_DETECTED",
            message="external close",
            symbol="BTCUSDT",
            trade_id="trade-1",
        ),
        PositionReconciliationFinding(
            code="EXCHANGE_POSITION_QUANTITY_MISMATCH",
            message="position mismatch",
            symbol="ETHUSDT",
            trade_id="trade-2",
        ),
    ]

    report = LifecycleMismatchDetectionService(
        _OrderSource(_order_report(OrderReconciliationState.DRIFT_DETECTED, order_findings)),
        _PositionSource(
            _position_report(
                PositionReconciliationState.DRIFT_DETECTED,
                position_findings,
            )
        ),
    ).latest()

    assert report.state is LifecycleReconciliationState.MISMATCH_DETECTED
    assert report.blocking is True
    assert report.finding_count == 5
    assert {item.category for item in report.findings} == {
        LifecycleMismatchCategory.PARTIAL_FILL,
        LifecycleMismatchCategory.EXTERNAL_CLOSE,
        LifecycleMismatchCategory.MISSING_PROTECTION,
        LifecycleMismatchCategory.EXCHANGE_RUNTIME_MISMATCH,
    }


def test_lifecycle_reconciliation_is_unavailable_until_both_sources_run() -> None:
    report = LifecycleMismatchDetectionService(
        _OrderSource(_order_report(OrderReconciliationState.NOT_RUN)),
        _PositionSource(_position_report(PositionReconciliationState.NOT_RUN)),
    ).latest()

    assert report.state is LifecycleReconciliationState.UNAVAILABLE
    assert report.blocking is True
    assert {item.code for item in report.findings} == {
        "ORDER_RECONCILIATION_NOT_READY",
        "POSITION_RECONCILIATION_NOT_READY",
    }
