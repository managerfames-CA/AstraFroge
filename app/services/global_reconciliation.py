"""Global fail-closed authority across every exchange reconciliation surface."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from threading import RLock

from app.schemas.global_reconciliation import (
    GlobalReconciliationReport,
    GlobalReconciliationState,
)
from app.schemas.lifecycle_reconciliation import LifecycleReconciliationState
from app.schemas.order_audit import OrderAuditState
from app.schemas.order_reconciliation import OrderReconciliationState
from app.schemas.position_reconciliation import PositionReconciliationState
from app.schemas.restart_recovery import RestartRecoveryState
from app.services.lifecycle_reconciliation import LifecycleMismatchDetectionService
from app.services.order_audit_runtime import RuntimeOrderAuditService
from app.services.order_reconciliation import ContinuousOrderReconciliationService
from app.services.position_reconciliation import ContinuousPositionReconciliationService
from app.services.protective_lifecycle import ProtectiveLifecycleVerificationService
from app.services.recovery import AutomationRecoveryGate
from app.services.restart_recovery import RestartRecoveryOwnershipService


class GlobalReconciliationSafetyService:
    """Prove all exchange-truth surfaces before and during automated execution."""

    def __init__(
        self,
        order_service: ContinuousOrderReconciliationService,
        position_service: ContinuousPositionReconciliationService,
        restart_service: RestartRecoveryOwnershipService,
        recovery_gate: AutomationRecoveryGate,
        *,
        protective_service: ProtectiveLifecycleVerificationService | None = None,
        interval_seconds: float = 15.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Global reconciliation interval must be positive")
        self._order = order_service
        self._position = position_service
        self._restart = restart_service
        self._protective = protective_service
        self._order_audit = RuntimeOrderAuditService.from_protective_service(
            protective_service,
            interval_seconds=interval_seconds,
        )
        self._gate = recovery_gate
        self._interval_seconds = interval_seconds
        self._now = now_provider or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._latest = GlobalReconciliationReport(
            state=GlobalReconciliationState.NOT_RUN,
            checked_at=self._now(),
            order_state=OrderReconciliationState.NOT_RUN,
            position_state=PositionReconciliationState.NOT_RUN,
            lifecycle_state=LifecycleReconciliationState.UNAVAILABLE,
            restart_state=RestartRecoveryState.NOT_READY,
            automation_ready=False,
            blocking=True,
            error_count=1,
            error_codes=["GLOBAL_RECONCILIATION_NOT_RUN"],
        )

    def latest(self) -> GlobalReconciliationReport:
        """Return the latest immutable global safety result."""

        with self._lock:
            return self._latest.model_copy(deep=True)

    def order_audit_service(self) -> RuntimeOrderAuditService | None:
        """Return the app-scoped BE-14 audit authority created at startup."""

        return self._order_audit

    async def run_forever(self) -> None:
        """Keep proving safety and observing lifecycle/audit evidence while blocked."""

        while True:
            if self._gate.snapshot().automation_ready:
                report = await asyncio.to_thread(self.reconcile)
                if report.blocking and self._protective is None and self._order_audit is None:
                    return
            else:
                observers = 0
                if self._protective is not None:
                    await asyncio.to_thread(self._protective.reconcile)
                    observers += 1
                if self._order_audit is not None:
                    await asyncio.to_thread(self._order_audit.reconcile)
                    observers += 1
                if observers == 0:
                    return
            await asyncio.sleep(self._interval_seconds)

    def reconcile(self) -> GlobalReconciliationReport:
        """Run one atomic fail-closed proof across all reconciliation sources."""

        checked_at = self._now()
        try:
            order_audit_report = (
                self._order_audit.reconcile() if self._order_audit is not None else None
            )
            protective_report = (
                self._protective.reconcile() if self._protective is not None else None
            )
            order_report = self._order.reconcile()
            position_report = self._position.reconcile()
            lifecycle_report = LifecycleMismatchDetectionService(
                self._order,
                self._position,
            ).latest()
            restart_report = self._restart.report()
        except Exception:
            return self._block_unavailable(checked_at)

        error_codes: list[str] = []
        if order_audit_report is not None:
            if order_audit_report.state is not OrderAuditState.READY:
                error_codes.append("ORDER_AUDIT_NOT_SAFE")
            if order_audit_report.blocking:
                error_codes.extend(item.code for item in order_audit_report.findings)

        if protective_report is not None and protective_report.blocking:
            error_codes.append("PROTECTIVE_LIFECYCLE_NOT_SAFE")
            error_codes.extend(item.code for item in protective_report.findings if item.blocking)

        if order_report.state is not OrderReconciliationState.IN_SYNC:
            error_codes.append("ORDER_RECONCILIATION_NOT_SAFE")
        if order_report.blocking:
            error_codes.extend(item.code for item in order_report.findings if item.blocking)

        if position_report.state is not PositionReconciliationState.IN_SYNC:
            error_codes.append("POSITION_RECONCILIATION_NOT_SAFE")
        if position_report.blocking:
            error_codes.extend(item.code for item in position_report.findings if item.blocking)

        if lifecycle_report.state is not LifecycleReconciliationState.IN_SYNC:
            error_codes.append("LIFECYCLE_RECONCILIATION_NOT_SAFE")
        if lifecycle_report.blocking:
            error_codes.extend(item.code for item in lifecycle_report.findings if item.blocking)

        if restart_report.state is not RestartRecoveryState.RECOVERED:
            error_codes.append("RESTART_RECOVERY_NOT_SAFE")
        if restart_report.blocking and restart_report.error is not None:
            error_codes.append(restart_report.error)

        normalized_errors = sorted(set(error_codes))
        blocking = bool(normalized_errors)
        report = GlobalReconciliationReport(
            state=(
                GlobalReconciliationState.BLOCKED if blocking else GlobalReconciliationState.SAFE
            ),
            checked_at=checked_at,
            order_state=order_report.state,
            position_state=position_report.state,
            lifecycle_state=lifecycle_report.state,
            restart_state=restart_report.state,
            automation_ready=(self._gate.snapshot().automation_ready and not blocking),
            blocking=blocking,
            error_count=len(normalized_errors),
            error_codes=normalized_errors,
        )
        self._publish(report)
        if blocking:
            self._gate.fail("GLOBAL_RECONCILIATION_UNSAFE")
            report = report.model_copy(update={"automation_ready": False})
            self._publish(report)
        return report

    def _block_unavailable(self, checked_at: datetime) -> GlobalReconciliationReport:
        report = GlobalReconciliationReport(
            state=GlobalReconciliationState.UNAVAILABLE,
            checked_at=checked_at,
            order_state=self._order.latest().state,
            position_state=self._position.latest().state,
            lifecycle_state=LifecycleReconciliationState.UNAVAILABLE,
            restart_state=RestartRecoveryState.NOT_READY,
            automation_ready=False,
            blocking=True,
            error_count=1,
            error_codes=["GLOBAL_RECONCILIATION_UNAVAILABLE"],
        )
        self._gate.fail("GLOBAL_RECONCILIATION_UNAVAILABLE")
        self._publish(report)
        return report

    def _publish(self, report: GlobalReconciliationReport) -> None:
        with self._lock:
            self._latest = report
