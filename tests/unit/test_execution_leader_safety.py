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

    def execute(self, statement: Any, params: Any = None) -> _ScalarResult:
        del params
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
    def __init__(self) -> None:
        self.activate_calls = 0

    def status(self) -> DemoExecutionStatusResponse:
        return DemoExecutionStatusResponse(
            state=DemoExecutionState.READY,
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
            count=1,
            plans=[
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
            ],
        )

    def activate(self, signal_id: str, request: Any = None) -> Any:
        del request
        self.activate_calls += 1
        return SimpleNamespace(signal_id=signal_id)

    def trades(self) -> Any:
        return SimpleNamespace(trades=[])

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
) -> LeaderValidatedExecutionService:
    return LeaderValidatedExecutionService(
        cast(DemoExecutionService, inner),
        gate,
        lease,
        recovery_required=True,
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
