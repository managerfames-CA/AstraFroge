"""Phase 1 startup recovery barrier regression tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.persistence.database import Persistence
from app.schemas.execution import DemoTradeLifecycle
from app.schemas.scanner import CandidateLifecycle, ScannerDirection, ScannerRunStatus, ScannerSetup
from app.schemas.signals import SignalLifecycle
from app.services.execution import DemoExecutionService
from app.services.recovery import (
    AutomationRecoveryGate,
    ExecutionLeaderLease,
    RecoveryGuardedExecutionService,
    StartupRecoveryCoordinator,
)
from app.services.scanner import ScannerService
from app.services.signals import SignalService


class _Lease:
    def __init__(self, *, acquired: bool = True) -> None:
        self.acquired = acquired
        self.released = False

    def acquire(self) -> bool:
        return self.acquired

    def release(self) -> None:
        self.released = True


class _Scanner:
    def __init__(
        self,
        *,
        candidates: list[Any] | None = None,
        status: ScannerRunStatus = ScannerRunStatus.COMPLETED,
    ) -> None:
        self.run_id = "recovery-run-1"
        self.completed_at = datetime.now(UTC)
        self._candidates = candidates or []
        self._status = status

    async def run_now(self) -> Any:
        return SimpleNamespace(
            run_id=self.run_id,
            status=self._status,
            completed_at=self.completed_at,
        )

    def candidates(self) -> list[Any]:
        return list(self._candidates)

    def risk_stop_price(self, candidate_id: str) -> None:
        del candidate_id
        return None


class _Signals:
    def __init__(self, records: list[Any] | None = None) -> None:
        self.records = records or []

    def signals(self) -> Any:
        return SimpleNamespace(signals=list(self.records))


class _Execution:
    def __init__(self, trades: list[Any] | None = None) -> None:
        self._trades = trades or []
        self.activate_calls = 0
        self.auto_calls = 0

    def trades(self) -> Any:
        return SimpleNamespace(trades=list(self._trades))

    def activate(self, signal_id: str, request: Any = None) -> Any:
        del request
        self.activate_calls += 1
        return SimpleNamespace(signal_id=signal_id)

    def auto_execute_pending(self) -> int:
        self.auto_calls += 1
        return 1


class _PrivateClient:
    def __init__(
        self,
        *,
        positions: list[dict[str, Any]] | None = None,
        fail: bool = False,
    ) -> None:
        self._positions = positions or []
        self._fail = fail

    def open_orders(self) -> list[dict[str, Any]]:
        return []

    def open_algo_orders(self) -> list[dict[str, Any]]:
        return []

    def positions(self) -> list[dict[str, Any]]:
        if self._fail:
            raise BinanceDemoPrivateClientError("demo unavailable")
        return list(self._positions)

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        raise AssertionError(f"query_order should not be reached: {symbol}:{orig_client_order_id}")

    def query_algo_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        raise AssertionError(
            f"query_algo_order should not be reached: {symbol}:{orig_client_order_id}"
        )


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        execution_enabled=True,
        execution_take_profit_r_multiple="2",
        scanner_auto_start=True,
        binance_demo_base_url="https://demo-fapi.binance.com",
        binance_demo_api_key="demo-key",
        binance_demo_api_secret="demo-secret",
    )


def _coordinator(
    *,
    gate: AutomationRecoveryGate,
    scanner: _Scanner | None = None,
    signals: _Signals | None = None,
    execution: _Execution | None = None,
    private_client: _PrivateClient | None = None,
    lease: _Lease | None = None,
) -> StartupRecoveryCoordinator:
    return StartupRecoveryCoordinator(
        settings=_settings(),
        gate=gate,
        leader_lease=cast(ExecutionLeaderLease, lease or _Lease()),
        scanner_service=cast(ScannerService, scanner or _Scanner()),
        signal_service=cast(SignalService, signals or _Signals()),
        execution_service=cast(DemoExecutionService, execution or _Execution()),
        private_client=private_client or _PrivateClient(),
        persistence=cast(Persistence, object()),
    )


def _guarded_execution(
    inner: _Execution,
    gate: AutomationRecoveryGate,
) -> RecoveryGuardedExecutionService:
    return RecoveryGuardedExecutionService(
        cast(DemoExecutionService, inner),
        gate,
        recovery_required=True,
    )


def _ready_gate() -> AutomationRecoveryGate:
    gate = AutomationRecoveryGate()
    gate.begin()
    gate.mark_exchange_reconciled()
    gate.mark_signals_revalidated()
    gate.mark_ready()
    return gate


def test_restart_with_persisted_active_signal_blocks_auto_execution_before_recovery() -> None:
    gate = AutomationRecoveryGate()
    inner = _Execution()
    service = _guarded_execution(inner, gate)

    with pytest.raises(AppError) as exc:
        service.auto_execute_pending()

    assert exc.value.code == "RECOVERY_NOT_COMPLETE"
    assert inner.auto_calls == 0


def test_recovery_complete_allows_valid_new_execution_path() -> None:
    gate = _ready_gate()
    inner = _Execution()
    service = _guarded_execution(inner, gate)

    result = service.activate("a" * 64)

    assert result.signal_id == "a" * 64
    assert inner.activate_calls == 1


def test_persisted_stale_active_signal_cannot_unlock_automation() -> None:
    gate = AutomationRecoveryGate()
    stale_signal = SimpleNamespace(
        lifecycle=SignalLifecycle.ACTIVE,
        candidate_id="stale-candidate",
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        scanner_lifecycle=CandidateLifecycle.QUALIFIED,
        entry_trigger_price=100,
        source_run_id="old-run",
        stop_loss_price=None,
    )
    coordinator = _coordinator(
        gate=gate,
        scanner=_Scanner(candidates=[]),
        signals=_Signals([stale_signal]),
    )

    assert asyncio.run(coordinator.recover()) is False
    snapshot = gate.snapshot()
    assert snapshot.automation_ready is False
    assert snapshot.recovery_error == "ACTIVE_SIGNAL_REVALIDATION_FAILED"


def test_exchange_local_position_mismatch_keeps_automation_locked() -> None:
    gate = AutomationRecoveryGate()
    local_trade = SimpleNamespace(
        lifecycle=DemoTradeLifecycle.OPEN,
        symbol="BTCUSDT",
        direction=ScannerDirection.LONG,
        executed_quantity=1,
        stop_client_order_id="stop-client",
        stop_order_id="201",
        take_profit_client_order_id="tp-client",
        take_profit_order_id="301",
    )
    private_client = _PrivateClient(positions=[])
    private_client.open_algo_orders = lambda: [  # type: ignore[method-assign]
        {
            "symbol": "BTCUSDT",
            "clientOrderId": "stop-client",
            "orderId": 201,
            "status": "NEW",
        },
        {
            "symbol": "BTCUSDT",
            "clientOrderId": "tp-client",
            "orderId": 301,
            "status": "NEW",
        },
    ]
    coordinator = _coordinator(
        gate=gate,
        execution=_Execution([local_trade]),
        private_client=private_client,
    )

    assert asyncio.run(coordinator.recover()) is False
    snapshot = gate.snapshot()
    assert snapshot.automation_ready is False
    assert snapshot.exchange_reconciled is False
    assert snapshot.recovery_error == "LOCAL_EXCHANGE_POSITION_MISMATCH"


def test_recovery_failure_is_fail_closed() -> None:
    gate = AutomationRecoveryGate()
    coordinator = _coordinator(gate=gate, private_client=_PrivateClient(fail=True))

    assert asyncio.run(coordinator.recover()) is False
    snapshot = gate.snapshot()
    assert snapshot.automation_ready is False
    assert snapshot.recovery_error == "EXCHANGE_RECONCILIATION_FAILED"


def test_direct_activation_cannot_bypass_recovery_barrier() -> None:
    gate = AutomationRecoveryGate()
    inner = _Execution()
    service = _guarded_execution(inner, gate)

    with pytest.raises(AppError) as exc:
        service.activate("b" * 64)

    assert exc.value.code == "RECOVERY_NOT_COMPLETE"
    assert inner.activate_calls == 0


def test_successful_empty_state_recovery_reaches_automation_ready() -> None:
    gate = AutomationRecoveryGate()
    coordinator = _coordinator(gate=gate)

    assert asyncio.run(coordinator.recover()) is True
    snapshot = gate.snapshot()
    assert snapshot.exchange_reconciled is True
    assert snapshot.signals_revalidated is True
    assert snapshot.automation_ready is True
    assert snapshot.recovery_error is None


class _AdvisoryState:
    held = False


class _ScalarResult:
    def __init__(self, value: bool) -> None:
        self._value = value

    def scalar(self) -> bool:
        return self._value


class _FakeConnection:
    def __init__(self, state: _AdvisoryState) -> None:
        self.state = state
        self.closed = False

    def execute(self, statement: Any, params: Any) -> _ScalarResult:
        del params
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            if self.state.held:
                return _ScalarResult(False)
            self.state.held = True
            return _ScalarResult(True)
        if "pg_advisory_unlock" in sql:
            self.state.held = False
            return _ScalarResult(True)
        raise AssertionError(sql)

    def close(self) -> None:
        self.closed = True


class _FakeEngine:
    def __init__(self, state: _AdvisoryState) -> None:
        self.state = state
        self.dialect = SimpleNamespace(name="postgresql")

    def connect(self) -> _FakeConnection:
        return _FakeConnection(self.state)


class _FakePersistence:
    def __init__(self, state: _AdvisoryState) -> None:
        self.engine = _FakeEngine(state)


def test_duplicate_execution_worker_ownership_cannot_be_acquired() -> None:
    state = _AdvisoryState()
    first = ExecutionLeaderLease(cast(Persistence, _FakePersistence(state)))
    second = ExecutionLeaderLease(cast(Persistence, _FakePersistence(state)))

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()
