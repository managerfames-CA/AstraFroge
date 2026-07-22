"""Focused BE-07 global reconciliation safety tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest

from app.schemas.global_reconciliation import GlobalReconciliationState
from app.schemas.lifecycle_reconciliation import LifecycleReconciliationState
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
from app.schemas.recovery import RecoveryState
from app.schemas.restart_recovery import RestartRecoveryReport, RestartRecoveryState
from app.services.global_reconciliation import GlobalReconciliationSafetyService
from app.services.order_reconciliation import ContinuousOrderReconciliationService
from app.services.position_reconciliation import ContinuousPositionReconciliationService
from app.services.recovery import AutomationRecoveryGate
from app.services.restart_recovery import RestartRecoveryOwnershipService

NOW = datetime(2026, 7, 19, 14, 20, tzinfo=UTC)


class _Order:
    def __init__(self, report: OrderReconciliationReport, *, fail: bool = False) -> None:
        self.report = report
        self.fail = fail
        self.calls = 0

    def reconcile(self) -> OrderReconciliationReport:
        self.calls += 1
        if self.fail:
            raise RuntimeError("order source failed")
        return self.report

    def latest(self) -> OrderReconciliationReport:
        return self.report


class _Position:
    def __init__(self, report: PositionReconciliationReport) -> None:
        self.report = report
        self.calls = 0

    def reconcile(self) -> PositionReconciliationReport:
        self.calls += 1
        return self.report

    def latest(self) -> PositionReconciliationReport:
        return self.report


class _Restart:
    def __init__(self, report: RestartRecoveryReport) -> None:
        self.report_value = report
        self.calls = 0

    def report(self) -> RestartRecoveryReport:
        self.calls += 1
        return self.report_value


def _ready_gate() -> AutomationRecoveryGate:
    gate = AutomationRecoveryGate()
    gate.begin()
    gate.mark_exchange_reconciled()
    gate.mark_signals_revalidated()
    gate.mark_ready()
    return gate


def _order(
    state: OrderReconciliationState = OrderReconciliationState.IN_SYNC,
    *,
    blocking: bool = False,
) -> OrderReconciliationReport:
    findings = (
        [
            OrderReconciliationFinding(
                code="ORDER_DRIFT",
                message="Order truth is unsafe",
            )
        ]
        if blocking
        else []
    )
    return OrderReconciliationReport(
        state=state,
        checked_at=NOW,
        local_open_trade_count=0,
        exchange_open_regular_order_count=0,
        exchange_open_algo_order_count=0,
        blocking=blocking,
        findings=findings,
    )


def _position(
    state: PositionReconciliationState = PositionReconciliationState.IN_SYNC,
    *,
    blocking: bool = False,
) -> PositionReconciliationReport:
    findings = (
        [
            PositionReconciliationFinding(
                code="POSITION_DRIFT",
                message="Position truth is unsafe",
            )
        ]
        if blocking
        else []
    )
    return PositionReconciliationReport(
        state=state,
        checked_at=NOW,
        local_open_trade_count=0,
        exchange_open_position_count=0,
        blocking=blocking,
        findings=findings,
    )


def _restart(
    state: RestartRecoveryState = RestartRecoveryState.RECOVERED,
    *,
    blocking: bool = False,
    error: str | None = None,
) -> RestartRecoveryReport:
    return RestartRecoveryReport(
        state=state,
        checked_at=NOW,
        recovery_state=RecoveryState.AUTOMATION_READY,
        exchange_reconciled=True,
        automation_ready=not blocking,
        recovered_open_trade_count=0,
        recovered_open_order_count=0,
        recovered_open_position_count=0,
        blocking=blocking,
        error=error,
    )


def _service(
    gate: AutomationRecoveryGate,
    *,
    order: _Order | None = None,
    position: _Position | None = None,
    restart: _Restart | None = None,
    interval_seconds: float = 15.0,
) -> GlobalReconciliationSafetyService:
    return GlobalReconciliationSafetyService(
        cast(ContinuousOrderReconciliationService, order or _Order(_order())),
        cast(ContinuousPositionReconciliationService, position or _Position(_position())),
        cast(RestartRecoveryOwnershipService, restart or _Restart(_restart())),
        gate,
        interval_seconds=interval_seconds,
        now_provider=lambda: NOW,
    )


def test_global_reconciliation_requires_positive_interval() -> None:
    with pytest.raises(ValueError, match="positive"):
        _service(_ready_gate(), interval_seconds=0)


def test_global_reconciliation_safe_keeps_automation_ready() -> None:
    gate = _ready_gate()
    order = _Order(_order())
    position = _Position(_position())
    restart = _Restart(_restart())
    service = _service(gate, order=order, position=position, restart=restart)

    report = service.reconcile()

    assert report.state is GlobalReconciliationState.SAFE
    assert report.lifecycle_state is LifecycleReconciliationState.IN_SYNC
    assert report.blocking is False
    assert report.error_codes == []
    assert report.automation_ready is True
    assert gate.snapshot().automation_ready is True
    assert order.calls == position.calls == restart.calls == 1
    assert service.latest() == report


def test_order_drift_fails_global_gate_closed() -> None:
    gate = _ready_gate()
    service = _service(
        gate,
        order=_Order(_order(OrderReconciliationState.DRIFT_DETECTED, blocking=True)),
    )

    report = service.reconcile()

    assert report.state is GlobalReconciliationState.BLOCKED
    assert report.blocking is True
    assert report.automation_ready is False
    assert "ORDER_RECONCILIATION_NOT_SAFE" in report.error_codes
    assert "ORDER_DRIFT" in report.error_codes
    assert "LIFECYCLE_RECONCILIATION_NOT_SAFE" in report.error_codes
    assert gate.snapshot().recovery_error == "GLOBAL_RECONCILIATION_UNSAFE"


def test_position_unavailable_fails_global_gate_closed() -> None:
    gate = _ready_gate()
    service = _service(
        gate,
        position=_Position(_position(PositionReconciliationState.UNAVAILABLE, blocking=True)),
    )

    report = service.reconcile()

    assert report.state is GlobalReconciliationState.BLOCKED
    assert "POSITION_RECONCILIATION_NOT_SAFE" in report.error_codes
    assert "POSITION_DRIFT" in report.error_codes
    assert gate.snapshot().automation_ready is False


def test_restart_recovery_mismatch_fails_global_gate_closed() -> None:
    gate = _ready_gate()
    service = _service(
        gate,
        restart=_Restart(
            _restart(
                RestartRecoveryState.BLOCKED,
                blocking=True,
                error="RECOVERED_OPEN_POSITION_SET_MISMATCH",
            )
        ),
    )

    report = service.reconcile()

    assert report.state is GlobalReconciliationState.BLOCKED
    assert "RESTART_RECOVERY_NOT_SAFE" in report.error_codes
    assert "RECOVERED_OPEN_POSITION_SET_MISMATCH" in report.error_codes
    assert gate.snapshot().automation_ready is False


def test_unexpected_source_failure_is_unavailable_and_fail_closed() -> None:
    gate = _ready_gate()
    service = _service(gate, order=_Order(_order(), fail=True))

    report = service.reconcile()

    assert report.state is GlobalReconciliationState.UNAVAILABLE
    assert report.error_codes == ["GLOBAL_RECONCILIATION_UNAVAILABLE"]
    assert report.automation_ready is False
    assert gate.snapshot().recovery_error == "GLOBAL_RECONCILIATION_UNAVAILABLE"


def test_global_monitor_stops_after_blocking_cycle() -> None:
    gate = _ready_gate()
    order = _Order(_order(OrderReconciliationState.DRIFT_DETECTED, blocking=True))
    service = _service(gate, order=order, interval_seconds=0.001)

    asyncio.run(service.run_forever())

    assert order.calls == 1
    assert gate.snapshot().automation_ready is False
