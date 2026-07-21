"""Additional branch coverage for the Phase 1 startup recovery safety boundary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.core.config import Settings
from app.persistence.database import Persistence
from app.schemas.execution import (
    DemoExecutionAccountResponse,
    DemoExecutionPlan,
    DemoExecutionPlanList,
    DemoExecutionState,
    DemoExecutionStatusResponse,
    DemoExecutionSummary,
    DemoPlanState,
    DemoTradeLifecycle,
    DemoTradeRecordList,
)
from app.schemas.risk import RiskDecision
from app.schemas.scanner import (
    CandidateLifecycle,
    ScannerDirection,
    ScannerGrade,
    ScannerRunStatus,
    ScannerSetup,
)
from app.schemas.signals import SignalLifecycle
from app.services.execution import DemoExecutionService
from app.services.recovery import (
    AutomationRecoveryGate,
    ExecutionLeaderLease,
    RecoveryFailure,
    RecoveryGuardedExecutionService,
    StartupRecoveryCoordinator,
)
from app.services.scanner import ScannerService
from app.services.signals import SignalService

NOW = datetime.now(UTC)
_PERSISTENCE_DEFAULT = object()


class _Lease:
    def __init__(self, acquired: bool = True) -> None:
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
        status: ScannerRunStatus = ScannerRunStatus.COMPLETED,
        completed_at: datetime | None = NOW,
        candidates: list[Any] | None = None,
        stop: Decimal | None = None,
    ) -> None:
        self.status_value = status
        self.completed_at = completed_at
        self.current_candidates = candidates or []
        self.stop = stop

    async def run_now(self) -> Any:
        return SimpleNamespace(
            run_id="recovery-run",
            status=self.status_value,
            completed_at=self.completed_at,
        )

    def candidates(self) -> list[Any]:
        return list(self.current_candidates)

    def risk_stop_price(self, candidate_id: str) -> Decimal | None:
        del candidate_id
        return self.stop


class _Signals:
    def __init__(self, records: list[Any] | None = None) -> None:
        self.records = records or []

    def signals(self) -> Any:
        return SimpleNamespace(signals=list(self.records))


class _Execution:
    def __init__(self, trades: list[Any] | None = None) -> None:
        self.trade_records = trades or []
        self.auto_calls = 0
        self.activate_calls = 0
        self.stored: Any = None
        self.marker = "delegated"

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
        executable = DemoExecutionPlan(
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
        watch = executable.model_copy(
            update={
                "signal_id": "b" * 64,
                "risk_decision": RiskDecision.WATCH,
                "plan_state": DemoPlanState.WATCH,
                "executable_now": False,
            }
        )
        return DemoExecutionPlanList(count=2, plans=[executable, watch])

    def trades(self) -> DemoTradeRecordList:
        return cast(DemoTradeRecordList, SimpleNamespace(trades=list(self.trade_records)))

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

    def auto_execute_pending(self) -> int:
        self.auto_calls += 1
        return 2

    def activate(self, signal_id: str, request: Any = None) -> Any:
        del request
        self.activate_calls += 1
        return SimpleNamespace(signal_id=signal_id)

    def get_trade(self, trade_id: str) -> Any:
        return SimpleNamespace(trade_id=trade_id)

    def store_trade(self, trade: Any) -> Any:
        self.stored = trade
        return trade


class _Private:
    def __init__(
        self,
        *,
        positions: list[dict[str, Any]] | None = None,
        order: dict[str, Any] | None = None,
        algos: dict[str, dict[str, Any]] | None = None,
        regular_open_orders: list[dict[str, Any]] | None = None,
        open_algo_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.position_rows = positions or []
        self.order = order or {}
        self.algos = algos or {}
        self.regular_open_order_rows = regular_open_orders or []
        self.open_algo_rows = open_algo_rows

    def positions(self) -> list[dict[str, Any]]:
        return list(self.position_rows)

    def open_orders(self) -> list[dict[str, Any]]:
        return list(self.regular_open_order_rows)

    def open_algo_orders(self) -> list[dict[str, Any]]:
        if self.open_algo_rows is not None:
            return list(self.open_algo_rows)
        return [dict(item) for item in self.algos.values()]

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        del symbol, orig_client_order_id
        return dict(self.order)

    def query_algo_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        del symbol
        return dict(self.algos[orig_client_order_id])


def _settings(
    *,
    execution_enabled: bool = True,
    scanner_auto_start: bool = True,
) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "scanner_auto_start": scanner_auto_start,
    }
    if execution_enabled:
        values.update(
            {
                "execution_enabled": True,
                "execution_take_profit_r_multiple": "2",
                "binance_demo_base_url": "https://demo-fapi.binance.com",
                "binance_demo_api_key": "demo-key",
                "binance_demo_api_secret": "demo-secret",
            }
        )
    return Settings(**values)


def _coordinator(
    *,
    gate: AutomationRecoveryGate | None = None,
    settings: Settings | None = None,
    lease: _Lease | None = None,
    scanner: _Scanner | None = None,
    signals: _Signals | None = None,
    execution: _Execution | None = None,
    private: _Private | None = None,
    persistence: Persistence | None | object = _PERSISTENCE_DEFAULT,
) -> StartupRecoveryCoordinator:
    persisted = None if persistence is None else cast(Persistence, persistence)
    return StartupRecoveryCoordinator(
        settings=settings or _settings(),
        gate=gate or AutomationRecoveryGate(),
        leader_lease=cast(ExecutionLeaderLease, lease or _Lease()),
        scanner_service=cast(ScannerService, scanner or _Scanner()),
        signal_service=cast(SignalService, signals or _Signals()),
        execution_service=cast(DemoExecutionService, execution or _Execution()),
        private_client=private,
        persistence=persisted,
    )


def test_gate_rejects_out_of_order_transitions_and_normalizes_failure() -> None:
    gate = AutomationRecoveryGate()
    with pytest.raises(RuntimeError):
        gate.mark_signals_revalidated()
    with pytest.raises(RuntimeError):
        gate.mark_ready()

    gate.fail("   ")
    assert gate.snapshot().recovery_error == "RECOVERY_FAILED"

    gate.begin()
    gate.mark_exchange_reconciled()
    gate.mark_signals_revalidated()
    gate.mark_ready()
    gate.require_ready()
    assert gate.snapshot().automation_ready is True


def test_leader_lease_rejects_missing_or_non_postgres_persistence() -> None:
    assert ExecutionLeaderLease(None).acquire() is False
    no_connection = ExecutionLeaderLease(None)
    no_connection.release()
    assert no_connection.held is False

    sqlite = SimpleNamespace(engine=SimpleNamespace(dialect=SimpleNamespace(name="sqlite")))
    lease = ExecutionLeaderLease(cast(Persistence, sqlite))
    assert lease.acquire() is False


def test_leader_lease_closes_connection_when_lock_query_fails() -> None:
    connection = SimpleNamespace(closed=False)

    def execute(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("database error")

    def close() -> None:
        connection.closed = True

    connection.execute = execute
    connection.close = close
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        connect=lambda: connection,
    )
    lease = ExecutionLeaderLease(cast(Persistence, SimpleNamespace(engine=engine)))

    with pytest.raises(RuntimeError, match="database error"):
        lease.acquire()
    assert connection.closed is True


def test_guarded_execution_exposes_recovery_status_and_blocks_plans() -> None:
    gate = AutomationRecoveryGate()
    inner = _Execution()
    service = RecoveryGuardedExecutionService(
        cast(DemoExecutionService, inner),
        gate,
        recovery_required=True,
    )

    status = service.status()
    assert status.state is DemoExecutionState.EXECUTION_LOCKED
    assert status.automation_ready is False
    plans = service.plans().plans
    assert plans[0].plan_state is DemoPlanState.BLOCKED
    assert plans[0].blocked_reason == "RECOVERY_NOT_COMPLETE"
    assert plans[1].plan_state is DemoPlanState.WATCH
    assert service.trades().trades == []
    assert service.account().can_trade is True
    assert service.get_trade("trade-1") is not None
    stored = SimpleNamespace(trade_id="trade-2")
    assert service.store_trade(cast(Any, stored)) is stored
    assert service.marker == "delegated"


def test_guarded_execution_delegates_when_recovery_not_required() -> None:
    gate = AutomationRecoveryGate()
    inner = _Execution()
    service = RecoveryGuardedExecutionService(
        cast(DemoExecutionService, inner),
        gate,
        recovery_required=False,
    )

    assert service.status().state is DemoExecutionState.READY
    assert service.plans().plans[0].plan_state is DemoPlanState.EXECUTABLE
    assert service.auto_execute_pending() == 2
    assert service.activate("c" * 64).signal_id == "c" * 64


@pytest.mark.parametrize(
    ("settings", "persistence", "lease", "expected"),
    [
        (_settings(execution_enabled=False), object(), _Lease(), "EXECUTION_DISABLED"),
        (_settings(), None, _Lease(), "PERSISTENCE_REQUIRED_FOR_AUTOMATION"),
        (_settings(scanner_auto_start=False), object(), _Lease(), "SCANNER_AUTOMATION_DISABLED"),
        (_settings(), object(), _Lease(False), "EXECUTION_LEADER_UNAVAILABLE"),
    ],
)
def test_recovery_prerequisites_fail_closed(
    settings: Settings,
    persistence: object | None,
    lease: _Lease,
    expected: str,
) -> None:
    gate = AutomationRecoveryGate()
    coordinator = _coordinator(
        gate=gate,
        settings=settings,
        persistence=persistence,
        lease=lease,
    )

    assert asyncio.run(coordinator.recover()) is False
    assert gate.snapshot().recovery_error == expected


def test_scanner_revalidation_failure_releases_leader_and_locks() -> None:
    gate = AutomationRecoveryGate()
    lease = _Lease()
    coordinator = _coordinator(
        gate=gate,
        lease=lease,
        scanner=_Scanner(status=ScannerRunStatus.FAILED),
        private=_Private(),
    )

    assert asyncio.run(coordinator.recover()) is False
    assert gate.snapshot().exchange_reconciled is True
    assert gate.snapshot().recovery_error == "SCANNER_REVALIDATION_FAILED"
    assert lease.released is True


def test_missing_private_client_fails_exchange_reconciliation() -> None:
    gate = AutomationRecoveryGate()
    coordinator = _coordinator(gate=gate, private=None)

    assert asyncio.run(coordinator.recover()) is False
    assert gate.snapshot().recovery_error == "DEMO_PRIVATE_API_NOT_CONFIGURED"


def _open_trade() -> Any:
    return SimpleNamespace(
        lifecycle=DemoTradeLifecycle.OPEN,
        symbol="BTCUSDT",
        direction=ScannerDirection.LONG,
        executed_quantity=Decimal("1"),
        client_order_id="entry-client",
        exchange_order_id="101",
        stop_client_order_id="stop-client",
        stop_order_id="201",
        take_profit_client_order_id="tp-client",
        take_profit_order_id="301",
    )


def _expected_open_algos() -> list[dict[str, Any]]:
    return [
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
            "status": "PARTIALLY_FILLED",
        },
    ]


def test_exchange_reconciliation_accepts_matching_position_and_protection() -> None:
    trade = _open_trade()
    private = _Private(
        positions=[{"symbol": "BTCUSDT", "positionAmt": "1"}],
        order={
            "clientOrderId": "entry-client",
            "orderId": 101,
            "status": "FILLED",
            "executedQty": "1",
        },
        algos={
            "stop-client": {
                "clientOrderId": "stop-client",
                "orderId": 201,
                "status": "NEW",
            },
            "tp-client": {
                "clientOrderId": "tp-client",
                "orderId": 301,
                "status": "PARTIALLY_FILLED",
            },
        },
        open_algo_rows=_expected_open_algos(),
    )
    gate = AutomationRecoveryGate()
    coordinator = _coordinator(
        gate=gate,
        execution=_Execution([trade]),
        private=private,
    )

    assert asyncio.run(coordinator.recover()) is True
    assert gate.snapshot().automation_ready is True
    coordinator.close()


def test_unexpected_regular_open_order_locks_automation() -> None:
    gate = AutomationRecoveryGate()
    coordinator = _coordinator(
        gate=gate,
        private=_Private(
            regular_open_orders=[
                {"symbol": "BTCUSDT", "clientOrderId": "unknown-order", "orderId": 999}
            ]
        ),
    )

    assert asyncio.run(coordinator.recover()) is False
    assert gate.snapshot().recovery_error == "UNEXPECTED_OPEN_REGULAR_ORDER"


def test_missing_or_unknown_open_algo_order_locks_automation() -> None:
    trade = _open_trade()
    gate = AutomationRecoveryGate()
    coordinator = _coordinator(
        gate=gate,
        execution=_Execution([trade]),
        private=_Private(
            positions=[{"symbol": "BTCUSDT", "positionAmt": "1"}],
            open_algo_rows=[
                {
                    "symbol": "BTCUSDT",
                    "clientOrderId": "unknown-protection",
                    "orderId": 999,
                    "status": "NEW",
                }
            ],
        ),
    )

    assert asyncio.run(coordinator.recover()) is False
    assert gate.snapshot().recovery_error == "OPEN_ALGO_ORDER_SET_MISMATCH"


@pytest.mark.parametrize(
    ("trades", "positions", "expected"),
    [
        (
            [_open_trade(), _open_trade()],
            [{"symbol": "BTCUSDT", "positionAmt": "1"}],
            "DUPLICATE_LOCAL_OPEN_TRADE",
        ),
        ([], [{"symbol": "", "positionAmt": "1"}], "EXCHANGE_POSITION_PAYLOAD_INVALID"),
        (
            [],
            [
                {"symbol": "BTCUSDT", "positionAmt": "1"},
                {"symbol": "BTCUSDT", "positionAmt": "2"},
            ],
            "DUPLICATE_EXCHANGE_POSITION",
        ),
        (
            [_open_trade()],
            [{"symbol": "BTCUSDT", "positionAmt": "-1"}],
            "LOCAL_EXCHANGE_POSITION_MISMATCH",
        ),
        (
            [_open_trade()],
            [{"symbol": "BTCUSDT", "positionAmt": "2"}],
            "LOCAL_EXCHANGE_POSITION_MISMATCH",
        ),
    ],
)
def test_exchange_reconciliation_mismatches_fail_closed(
    trades: list[Any],
    positions: list[dict[str, Any]],
    expected: str,
) -> None:
    open_algo_rows = _expected_open_algos() if len(trades) == 1 else []
    gate = AutomationRecoveryGate()
    coordinator = _coordinator(
        gate=gate,
        execution=_Execution(trades),
        private=_Private(positions=positions, open_algo_rows=open_algo_rows),
    )

    assert asyncio.run(coordinator.recover()) is False
    assert gate.snapshot().recovery_error == expected


def test_zero_exchange_position_is_ignored() -> None:
    gate = AutomationRecoveryGate()
    coordinator = _coordinator(
        gate=gate,
        private=_Private(positions=[{"symbol": "BTCUSDT", "positionAmt": "0"}]),
    )

    assert asyncio.run(coordinator.recover()) is True
    assert gate.snapshot().automation_ready is True


def test_entry_and_protection_verifiers_reject_exchange_mismatch() -> None:
    trade = _open_trade()
    with pytest.raises(RecoveryFailure, match="ENTRY_ORDER_RECONCILIATION_MISMATCH"):
        StartupRecoveryCoordinator._verify_entry_order(
            {
                "clientOrderId": "wrong",
                "orderId": 101,
                "status": "FILLED",
                "executedQty": "1",
            },
            trade,
        )
    with pytest.raises(RecoveryFailure, match="ENTRY_ORDER_RECONCILIATION_MISMATCH"):
        StartupRecoveryCoordinator._verify_entry_order(
            {
                "clientOrderId": "entry-client",
                "orderId": 101,
                "status": "FILLED",
                "executedQty": "2",
            },
            trade,
        )
    with pytest.raises(RecoveryFailure, match="PROTECTIVE_ORDER_RECONCILIATION_MISMATCH"):
        StartupRecoveryCoordinator._verify_protective_order(
            {"clientOrderId": "stop-client", "orderId": 201, "status": "CANCELED"},
            expected_client_order_id="stop-client",
            expected_order_id="201",
        )


def _candidate(*, expiry: datetime | None = None) -> Any:
    return SimpleNamespace(
        candidate_id="candidate-1",
        lifecycle=CandidateLifecycle.QUALIFIED,
        qualification_expires_at=expiry or NOW + timedelta(minutes=10),
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        entry_trigger_price=Decimal("100"),
    )


def _signal() -> Any:
    return SimpleNamespace(
        lifecycle=SignalLifecycle.ACTIVE,
        candidate_id="candidate-1",
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        scanner_lifecycle=CandidateLifecycle.QUALIFIED,
        entry_trigger_price=Decimal("100"),
        source_run_id="recovery-run",
        stop_loss_price=Decimal("99"),
    )


def test_signal_revalidation_accepts_current_version() -> None:
    coordinator = _coordinator(
        scanner=_Scanner(candidates=[_candidate()], stop=Decimal("99")),
        signals=_Signals([_signal()]),
        private=_Private(),
    )

    coordinator._revalidate_signals("recovery-run", NOW)


@pytest.mark.parametrize(
    ("signal_update", "candidate_update", "run_id", "expected"),
    [
        (
            {},
            {"qualification_expires_at": NOW},
            "recovery-run",
            "ACTIVE_SIGNAL_EXPIRED_DURING_RECOVERY",
        ),
        (
            {"direction": ScannerDirection.SHORT},
            {},
            "recovery-run",
            "ACTIVE_SIGNAL_CONTRACT_MISMATCH",
        ),
        (
            {"scanner_lifecycle": CandidateLifecycle.WATCH_NEAR},
            {},
            "recovery-run",
            "ACTIVE_SIGNAL_LIFECYCLE_MISMATCH",
        ),
        (
            {"entry_trigger_price": Decimal("101")},
            {},
            "recovery-run",
            "ACTIVE_SIGNAL_PLAN_VERSION_MISMATCH",
        ),
        ({}, {}, "other-run", "ACTIVE_SIGNAL_NOT_CURRENT_RUN"),
        (
            {"stop_loss_price": Decimal("98")},
            {},
            "recovery-run",
            "ACTIVE_SIGNAL_PLAN_VERSION_MISMATCH",
        ),
    ],
)
def test_signal_revalidation_rejects_stale_contract_variants(
    signal_update: dict[str, Any],
    candidate_update: dict[str, Any],
    run_id: str,
    expected: str,
) -> None:
    signal = _signal()
    candidate = _candidate()
    for key, value in signal_update.items():
        setattr(signal, key, value)
    for key, value in candidate_update.items():
        setattr(candidate, key, value)
    coordinator = _coordinator(
        scanner=_Scanner(candidates=[candidate], stop=Decimal("99")),
        signals=_Signals([signal]),
        private=_Private(),
    )

    with pytest.raises(RecoveryFailure, match=expected):
        coordinator._revalidate_signals(run_id, NOW)


def test_decimal_rejects_invalid_and_non_finite_exchange_values() -> None:
    with pytest.raises(RecoveryFailure, match="EXCHANGE_POSITION_PAYLOAD_INVALID"):
        StartupRecoveryCoordinator._decimal("bad", "positionAmt")
    with pytest.raises(RecoveryFailure, match="EXCHANGE_POSITIONAMT_INVALID"):
        StartupRecoveryCoordinator._decimal("NaN", "positionAmt")
