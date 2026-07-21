"""Fail-closed validation of the PostgreSQL execution-leader advisory lock."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.errors import AppError
from app.schemas.execution import (
    DemoExecutionActivateRequest,
    DemoExecutionPlanList,
    DemoExecutionState,
    DemoExecutionStatusResponse,
    DemoPlanState,
    DemoTradeRecord,
)
from app.services.execution import DemoExecutionService
from app.services.recovery import (
    _EXECUTION_LEADER_ADVISORY_LOCK_KEY,
    AutomationRecoveryGate,
    ExecutionLeaderLease,
    RecoveryGuardedExecutionService,
)

_LEADER_LOST_CODE = "EXECUTION_LEADER_LOST"
_ADVISORY_CLASS_ID = _EXECUTION_LEADER_ADVISORY_LOCK_KEY >> 32
_ADVISORY_OBJECT_ID = _EXECUTION_LEADER_ADVISORY_LOCK_KEY & 0xFFFFFFFF


class ExecutionLeaderLost(RuntimeError):
    """The dedicated PostgreSQL session no longer proves execution ownership."""


class ValidatedExecutionLeaderLease(ExecutionLeaderLease):
    """Session advisory lock whose continued ownership is actively provable."""

    def acquire(self) -> bool:
        """Acquire only from an explicit recovery flow; never reacquire during validation."""

        if self._connection is not None:
            try:
                self.require_valid()
                return True
            except ExecutionLeaderLost:
                pass
        return super().acquire()

    @property
    def held(self) -> bool:
        """Return true only when the dedicated session still proves the exact advisory lock."""

        try:
            self.require_valid()
        except ExecutionLeaderLost:
            return False
        return True

    def require_valid(self) -> None:
        """Prove the same live DB session still owns the exact advisory lock."""

        connection = self._connection
        if connection is None:
            raise ExecutionLeaderLost("Execution leader is not acquired")
        if connection.closed or connection.invalidated:
            self._discard_lost_connection()
            raise ExecutionLeaderLost("Execution leader database session is unavailable")

        try:
            owned = bool(
                connection.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM pg_locks
                            WHERE locktype = 'advisory'
                              AND pid = pg_backend_pid()
                              AND granted
                              AND classid::bigint = :class_id
                              AND objid::bigint = :object_id
                              AND objsubid = 1
                        )
                        """
                    ),
                    {
                        "class_id": _ADVISORY_CLASS_ID,
                        "object_id": _ADVISORY_OBJECT_ID,
                    },
                ).scalar()
            )
        except Exception as exc:
            self._discard_lost_connection()
            raise ExecutionLeaderLost(
                "Execution leader database session validation failed"
            ) from exc

        if not owned:
            self._discard_lost_connection()
            raise ExecutionLeaderLost("Execution advisory-lock ownership was lost")

    def _discard_lost_connection(self) -> None:
        """Forget a lost session without attempting advisory-lock reacquisition."""

        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            connection.close()
        except Exception:
            pass


def validate_leader_or_fail_closed(
    gate: AutomationRecoveryGate,
    lease: ValidatedExecutionLeaderLease,
) -> bool:
    """Validate current ownership and make any detected loss permanently fail closed."""

    if not gate.snapshot().automation_ready:
        return False
    try:
        lease.require_valid()
    except ExecutionLeaderLost:
        gate.fail(_LEADER_LOST_CODE)
        return False
    return True


class LeaderValidatedExecutionService(RecoveryGuardedExecutionService):
    """Require recovery readiness plus current leader ownership for every new entry."""

    def __init__(
        self,
        inner: DemoExecutionService,
        gate: AutomationRecoveryGate,
        leader_lease: ValidatedExecutionLeaderLease,
        *,
        recovery_required: bool,
    ) -> None:
        super().__init__(inner, gate, recovery_required=recovery_required)
        self._leader_lease = leader_lease

    def _require_new_entry_permission(self) -> None:
        if not self._recovery_required:
            return
        self._gate.require_ready()
        if validate_leader_or_fail_closed(self._gate, self._leader_lease):
            return
        raise AppError(
            status_code=409,
            code=_LEADER_LOST_CODE,
            message=(
                "Automated Demo entry is locked because current PostgreSQL execution-leader "
                "ownership can no longer be proven; controlled recovery is required"
            ),
        )

    def status(self) -> DemoExecutionStatusResponse:
        if self._recovery_required and self._gate.snapshot().automation_ready:
            validate_leader_or_fail_closed(self._gate, self._leader_lease)
        return super().status()

    def plans(self) -> DemoExecutionPlanList:
        if self._recovery_required and self._gate.snapshot().automation_ready:
            validate_leader_or_fail_closed(self._gate, self._leader_lease)
        return super().plans()

    def auto_execute_pending(self) -> int:
        """Validate ownership for the cycle and again before every individual activation."""

        self._require_new_entry_permission()
        if self.status().state is not DemoExecutionState.READY:
            return 0

        activated = 0
        for plan in self.plans().plans:
            if plan.plan_state is not DemoPlanState.EXECUTABLE:
                continue
            try:
                self.activate(plan.signal_id)
            except AppError as exc:
                if exc.code in {_LEADER_LOST_CODE, "RECOVERY_NOT_COMPLETE"}:
                    raise
                continue
            activated += 1
        return activated

    def activate(
        self,
        signal_id: str,
        request: DemoExecutionActivateRequest | None = None,
    ) -> DemoTradeRecord:
        self._require_new_entry_permission()
        return self._inner.activate(signal_id, request)

    def __getattr__(self, name: str) -> Any:
        return super().__getattr__(name)
