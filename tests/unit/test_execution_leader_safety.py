"""P0 regression tests for continuous PostgreSQL execution-leader ownership."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.core.errors import AppError
from app.persistence.database import Persistence
from app.schemas.execution import (
    DemoExecutionAccountResponse,
    DemoExecutionPlan,
    DemoExecutionPlanList,
    DemoExecutionState,
    DemoExecutionStatusResponse,
    DemoExecutionSummary,
    DemoPlanState,
)
from app.schemas.risk import RiskDecision
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.signals import SignalLifecycle
from app.services.execution import DemoExecutionService
from app.services.execution_leader_safety import (
    LeaderValidatedExecutionService,
    ValidatedExecutionLeaderLease,
    validate_leader_or_fail_closed,
    ExecutionLeaderLost,
)
from app.services.recovery import AutomationRecoveryGate
from tests.unit.scanner_test_support import NOW


class _ScalarResult:
    def __init__(self, value: bool) -> None:
        self._value = value

    def scalar(self) -> bool:
        return self._value


class _SharedAdvisoryState:
    def __init__(self) -> None:
        self.owner_id: int | None = None
        self.next_connection_id = 1


class _FakeConnection:
    def __init__(self, state: _SharedAdvisoryState) -> None:
        self.state = state
        self.connection_id = state.next_connection_id
        state.next_connection_id += 1
        self.closed = False
        self.invalidated = False
        self.should_raise_on_execute = False
        self.should_raise_on_close = False

    def execute(self, statement: Any, params: Any = None) -> _ScalarResult:
        del params
        if self.should_raise_on_execute:
            raise RuntimeError("Database execution error simulated")
        if self.closed or self.invalidated:
            raise RuntimeError("database connection lost")
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            if self.state.owner_id is None or self.state.owner_id == self.connection_id:
                self.state.owner_id = self.connection_id
                return _ScalarResult(True)
            return _ScalarResult(False)
        if "FROM pg_locks" in sql:
            return _ScalarResult(self.state.owner_id == self.connection_id)
        if "pg_advisory_unlock" in sql:
            owned = self.state.owner_id == self.connection_id
            if owned:
                self.state.owner_id = None
            return _ScalarResult(owned)
        raise AssertionError(sql)

    def drop_database_session(self) -> None:
        if self.state.owner_id == self.connection_id:
            self.state.owner_id = None
        self.invalidated = True

    def close(self) -> None:
        if self.should_raise_on_close:
            raise RuntimeError("Database close error simulated")
        if self.state.owner_id == self.connection_id:
            self.state.owner_id = None
        self.closed = True


class _FakeEngine:
    def __init__(self, state: _SharedAdvisoryState) -> None:
        self.state = state
        self.dialect = SimpleNamespace(name="postgresql")
        self.connections: list[_FakeConnection] = []

    def connect(self) -> _FakeConnection:
        connection = _FakeConnection(self.state)
        self.connections.append(connection)
        return connection


class _FakePersistence:
    def __init__(self, state: _SharedAdvisoryState) -> None:
        self.engine = _FakeEngine(state)


class _Execution:
    def __init__(self, state: DemoExecutionState = DemoExecutionState.READY) -> None:
        self.activate_calls = 0
        self.state = state
        self.plans_list = [
            DemoExecutionPlan(
                signal_id="a" * 64,
                symbol="BTCUSDT",
                direction=ScannerDirection.LONG,
                setup=ScannerSetup.TREND_PULLBACK,
                setup_name="Trend Pullback",
                signal_lifecycle=SignalLifecycle.ACTIVE,
                risk_decision=RiskDecision.APPROVED,
                plan_state=DemoPlanState.EXECUTABLE,
                grade=ScannerGrade.A,
                score=86,
                confidence=75,
                entry_trigger_price=Decimal("100"),
                stop_loss_price=Decimal("99"),
                recommended_quantity=Decimal("1"),
                take_profit_r_multiple=Decimal("2"),
                executable_now=True,
                updated_at=NOW,
            )
        ]

    def status(self) -> DemoExecutionStatusResponse:
        return DemoExecutionStatusResponse(
            state=self.state,
            execution_enabled=True,
            demo_credentials_configured=True,
            private_api_available=True,
            risk_engine_state="READY",
            take_profit_r_multiple=Decimal("2"),
            max_open_trades_limit=4,
            tracked_trade_count=0,
            available_tracking_slots=4,
            summary=DemoExecutionSummary(),
        )

    def plans(self) -> DemoExecutionPlanList:
        return DemoExecutionPlanList(
            count=len(self.plans_list),
            plans=self.plans_list,
        )

    def activate(self, signal_id: str, request: Any = None) -> Any:
        del request
        self.activate_calls += 1
        if signal_id == "raise_generic_app_error":
            raise AppError(status_code=400, code="GENERIC_ERROR", message="Generic error")
        if signal_id == "raise_recovery_not_complete":
            raise AppError(status_code=409, code="RECOVERY_NOT_COMPLETE", message="Recovery not complete")
        return SimpleNamespace(signal_id=signal_id)

    def trades(self) -> Any:
        return SimpleNamespace(trades=["mock_trade"])

    def account(self) -> DemoExecutionAccountResponse:
        return DemoExecutionAccountResponse(
            demo_private_execution_ready=True,
            can_trade=True,
            updated_at=NOW,
            total_wallet_balance_usdt=Decimal("1000"),
            available_balance_usdt=Decimal("1000"),
            total_unrealized_pnl_usdt=Decimal("0"),
            balances=[],
            open_positions=[],
        )


def _ready_gate() -> AutomationRecoveryGate:
    gate = AutomationRecoveryGate()
    gate.begin()
    gate.mark_exchange_reconciled()
    gate.mark_signals_revalidated()
    gate.mark_ready()
    return gate


def _lease(state: _SharedAdvisoryState) -> tuple[ValidatedExecutionLeaderLease, _FakePersistence]:
    persistence = _FakePersistence(state)
    lease = ValidatedExecutionLeaderLease(cast(Persistence, persistence))
    assert lease.acquire() is True
    return lease, persistence


def _service(
    gate: AutomationRecoveryGate,
    lease: ValidatedExecutionLeaderLease,
    inner: _Execution,
    recovery_required: bool = True,
) -> LeaderValidatedExecutionService:
    return LeaderValidatedExecutionService(
        cast(DemoExecutionService, inner),
        gate,
        lease,
        recovery_required=recovery_required,
    )


def test_activate_blocks_after_leader_database_session_loss() -> None:
    state = _SharedAdvisoryState()
    lease, persistence = _lease(state)
    gate = _ready_gate()
    inner = _Execution()
    service = _service(gate, lease, inner)

    persistence.engine.connections[0].drop_database_session()

    with pytest.raises(AppError) as exc:
        service.activate("a" * 64)

    assert exc.value.code == "EXECUTION_LEADER_LOST"
    assert inner.activate_calls == 0
    assert gate.snapshot().automation_ready is False
    assert gate.snapshot().recovery_error == "EXECUTION_LEADER_LOST"


def test_auto_execute_pending_blocks_after_leader_database_session_loss() -> None:
    state = _SharedAdvisoryState()
    lease, persistence = _lease(state)
    gate = _ready_gate()
    inner = _Execution()
    service = _service(gate, lease, inner)

    persistence.engine.connections[0].drop_database_session()

    with pytest.raises(AppError) as exc:
        service.auto_execute_pending()

    assert exc.value.code == "EXECUTION_LEADER_LOST"
    assert inner.activate_calls == 0
    assert gate.snapshot().automation_ready is False


def test_instance_b_can_acquire_after_a_loses_session_but_a_remains_blocked() -> None:
    state = _SharedAdvisoryState()
    lease_a, persistence_a = _lease(state)
    gate_a = _ready_gate()
    inner_a = _Execution()
    service_a = _service(gate_a, lease_a, inner_a)

    persistence_a.engine.connections[0].drop_database_session()

    persistence_b = _FakePersistence(state)
    lease_b = ValidatedExecutionLeaderLease(cast(Persistence, persistence_b))
    assert lease_b.acquire() is True
    lease_b.require_valid()

    with pytest.raises(AppError) as exc:
        service_a.activate("a" * 64)

    assert exc.value.code == "EXECUTION_LEADER_LOST"
    assert gate_a.snapshot().automation_ready is False
    assert inner_a.activate_calls == 0
    assert lease_b.held is True
    lease_b.release()


def test_detected_leader_loss_never_leaves_automation_logically_ready() -> None:
    state = _SharedAdvisoryState()
    lease, persistence = _lease(state)
    gate = _ready_gate()

    persistence.engine.connections[0].drop_database_session()

    assert validate_leader_or_fail_closed(gate, lease) is False
    snapshot = gate.snapshot()
    assert snapshot.automation_ready is False
    assert snapshot.recovery_error == "EXECUTION_LEADER_LOST"
    assert lease.held is False


def test_valid_leader_and_recovery_ready_still_allow_execution() -> None:
    state = _SharedAdvisoryState()
    lease, _ = _lease(state)
    gate = _ready_gate()
    inner = _Execution()
    service = _service(gate, lease, inner)

    result = service.activate("a" * 64)

    assert result.signal_id == "a" * 64
    assert inner.activate_calls == 1
    assert gate.snapshot().automation_ready is True
    assert lease.held is True
    lease.release()


# --- Additional unit tests for 100% code coverage ---

def test_acquire_already_has_valid_connection() -> None:
    state = _SharedAdvisoryState()
    lease, _ = _lease(state)
    # lease._connection is already valid, so calling acquire() should reuse it and return True
    assert lease.acquire() is True


def test_acquire_has_invalid_connection() -> None:
    state = _SharedAdvisoryState()
    lease, persistence = _lease(state)
    # Invalidate connection
    persistence.engine.connections[0].drop_database_session()
    # lease._connection is set but invalid. Calling acquire() should catch ExecutionLeaderLost and call super().acquire()
    assert lease.acquire() is True
    assert lease._connection is not None


def test_require_valid_unacquired() -> None:
    state = _SharedAdvisoryState()
    persistence = _FakePersistence(state)
    lease = ValidatedExecutionLeaderLease(cast(Persistence, persistence))
    # Connection is None
    with pytest.raises(ExecutionLeaderLost) as exc:
        lease.require_valid()
    assert "Execution leader is not acquired" in str(exc.value)


def test_require_valid_connection_closed() -> None:
    state = _SharedAdvisoryState()
    lease, persistence = _lease(state)
    persistence.engine.connections[0].close()
    with pytest.raises(ExecutionLeaderLost) as exc:
        lease.require_valid()
    assert "Execution leader database session is unavailable" in str(exc.value)


def test_require_valid_execute_raises_exception() -> None:
    state = _SharedAdvisoryState()
    lease, persistence = _lease(state)
    persistence.engine.connections[0].should_raise_on_execute = True
    with pytest.raises(ExecutionLeaderLost) as exc:
        lease.require_valid()
    assert "Execution leader database session validation failed" in str(exc.value)


def test_require_valid_not_owned() -> None:
    state = _SharedAdvisoryState()
    lease, _ = _lease(state)
    # Manually clear the lock owner ID in shared state so that owned becomes False
    state.owner_id = 999
    with pytest.raises(ExecutionLeaderLost) as exc:
        lease.require_valid()
    assert "Execution advisory-lock ownership was lost" in str(exc.value)


def test_discard_lost_connection_none() -> None:
    state = _SharedAdvisoryState()
    persistence = _FakePersistence(state)
    lease = ValidatedExecutionLeaderLease(cast(Persistence, persistence))
    # _connection is None, calling discard should be safe and return None
    assert lease._discard_lost_connection() is None


def test_discard_lost_connection_close_raises() -> None:
    state = _SharedAdvisoryState()
    lease, persistence = _lease(state)
    persistence.engine.connections[0].should_raise_on_close = True
    # Discard should silently suppress exceptions raised during close()
    lease._discard_lost_connection()
    assert lease._connection is None


def test_validate_leader_or_fail_closed_gate_not_ready() -> None:
    state = _SharedAdvisoryState()
    lease, _ = _lease(state)
    gate = AutomationRecoveryGate()  # Not ready
    assert validate_leader_or_fail_closed(gate, lease) is False


def test_service_recovery_not_required() -> None:
    state = _SharedAdvisoryState()
    lease, _ = _lease(state)
    gate = AutomationRecoveryGate()
    inner = _Execution()
    # service has recovery_required=False
    service = _service(gate, lease, inner, recovery_required=False)
    # Should bypass _require_new_entry_permission checks completely
    result = service.activate("a" * 64)
    assert result.signal_id == "a" * 64


def test_service_status_validation() -> None:
    state = _SharedAdvisoryState()
    lease, persistence = _lease(state)
    gate = _ready_gate()
    inner = _Execution()
    service = _service(gate, lease, inner)

    # Calling status when lease is valid
    resp = service.status()
    assert resp.state == DemoExecutionState.READY

    # Lose lease, status call should fail-closed the gate but still return response
    persistence.engine.connections[0].drop_database_session()
    resp2 = service.status()
    assert gate.snapshot().automation_ready is False


def test_service_plans_validation() -> None:
    state = _SharedAdvisoryState()
    lease, persistence = _lease(state)
    gate = _ready_gate()
    inner = _Execution()
    service = _service(gate, lease, inner)

    # Calling plans when lease is valid
    plans = service.plans()
    assert plans.count == 1

    # Lose lease, plans call should fail-closed the gate but still return response
    persistence.engine.connections[0].drop_database_session()
    service.plans()
    assert gate.snapshot().automation_ready is False


def test_service_delegated_getattr() -> None:
    state = _SharedAdvisoryState()
    lease, _ = _lease(state)
    gate = _ready_gate()
    inner = _Execution()
    service = _service(gate, lease, inner)

    # Testing delegate getattr
    assert service.trades().trades == ["mock_trade"]
    assert service.account().total_wallet_balance_usdt == Decimal("1000")


def test_auto_execute_pending_not_ready_status() -> None:
    state = _SharedAdvisoryState()
    lease, _ = _lease(state)
    gate = _ready_gate()
    inner = _Execution(state=DemoExecutionState.EXECUTION_LOCKED)
    service = _service(gate, lease, inner)

    # If status state is EXECUTION_LOCKED, it should return 0
    assert service.auto_execute_pending() == 0


def test_auto_execute_pending_plan_not_executable() -> None:
    state = _SharedAdvisoryState()
    lease, _ = _lease(state)
    gate = _ready_gate()
    inner = _Execution()
    inner.plans_list[0].plan_state = DemoPlanState.BLOCKED
    service = _service(gate, lease, inner)

    # If plans are not EXECUTABLE, return 0
    assert service.auto_execute_pending() == 0


def test_auto_execute_pending_activate_raises_app_error_generic() -> None:
    state = _SharedAdvisoryState()
    lease, _ = _lease(state)
    gate = _ready_gate()
    inner = _Execution()
    inner.plans_list[0].signal_id = "raise_generic_app_error"
    service = _service(gate, lease, inner)

    # Should catch the generic AppError and continue (since no other plans exist, returns 0)
    assert service.auto_execute_pending() == 0


def test_auto_execute_pending_activate_raises_recovery_not_complete() -> None:
    state = _SharedAdvisoryState()
    lease, _ = _lease(state)
    gate = _ready_gate()
    inner = _Execution()
    inner.plans_list[0].signal_id = "raise_recovery_not_complete"
    service = _service(gate, lease, inner)

    # Should raise the RECOVERY_NOT_COMPLETE AppError
    with pytest.raises(AppError) as exc:
        service.auto_execute_pending()
    assert exc.value.code == "RECOVERY_NOT_COMPLETE"


def test_status_and_plans_with_recovery_not_required() -> None:
    state = _SharedAdvisoryState()
    lease, _ = _lease(state)
    gate = _ready_gate()
    inner = _Execution()
    service = _service(gate, lease, inner, recovery_required=False)

    # Call status and plans with recovery_required=False to cover the False branch
    resp = service.status()
    assert resp.state == DemoExecutionState.READY
    plans = service.plans()
    assert plans.count == 1


def test_auto_execute_pending_success_activation() -> None:
    state = _SharedAdvisoryState()
    lease, _ = _lease(state)
    gate = _ready_gate()
    inner = _Execution()
    service = _service(gate, lease, inner)

    # Test successful activation loop where activated is incremented
    assert service.auto_execute_pending() == 1
    assert inner.activate_calls == 1


def test_getattr_fallback_raises_attribute_error() -> None:
    state = _SharedAdvisoryState()
    lease, _ = _lease(state)
    gate = _ready_gate()
    inner = _Execution()
    service = _service(gate, lease, inner)

    # Calling a non-existent attribute to trigger __getattr__ and expect AttributeError
    with pytest.raises(AttributeError):
        _ = service.non_existent_attribute_name_xyz
