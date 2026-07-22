"""Phase 5 durable command and single-worker safety tests."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect as sqlalchemy_inspect

from app.api.v1.routes import execution as execution_routes
from app.core.config import Settings
from app.core.errors import AppError
from app.main import create_app
from app.persistence.database import Persistence
from app.persistence.execution_command_repository import (
    ExecutionCommandRepository,
    IllegalExecutionCommandTransition,
)
from app.schemas.execution import DemoProtectionState, DemoTradeLifecycle, DemoTradeRecord
from app.schemas.execution_command import ExecutionCommandState
from app.schemas.risk import RiskAssessment, RiskAssessmentList, RiskDecision
from app.schemas.scanner import (
    CandidateLifecycle,
    ScannerDirection,
    ScannerGrade,
    ScannerSetup,
)
from app.schemas.signal_decision import EntryTriggerStatus, SignalDecisionStatus
from app.schemas.signals import SignalLifecycle, SignalRecord
from app.services.account_snapshot import (
    AccountPositionSnapshot,
    AccountSnapshot,
    AccountSnapshotStatus,
)
from app.services.execution_command import ExecutionCommandService
from app.services.execution_leader_safety import ExecutionLeaderLost
from app.services.execution_worker import DemoExecutionWorker
from app.services.recovery import AutomationRecoveryGate
from app.services.signal_decision import SignalDecisionEngine

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
SIGNAL_ID = "a" * 64


def _settings(*, enabled: bool = False) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        execution_enabled=enabled,
        execution_take_profit_r_multiple=Decimal("2"),
        scanner_auto_start=False,
    )


def _signal(
    *,
    signal_id: str = SIGNAL_ID,
    grade: ScannerGrade = ScannerGrade.A_PLUS,
    lifecycle: SignalLifecycle = SignalLifecycle.ACTIVE,
    decision_status: SignalDecisionStatus = SignalDecisionStatus.READY,
    trigger_status: EntryTriggerStatus = EntryTriggerStatus.READY,
    ready: bool = True,
    selected: bool = True,
    entry_ready: bool = True,
    decision_fresh: bool = True,
    decision_key: str | None = "d" * 64,
    source_snapshot_version: str | None = "s" * 64,
    expires_at: datetime = NOW + timedelta(minutes=15),
    watch_reasons: list[str] | None = None,
    rejection_reasons: list[str] | None = None,
    direction: ScannerDirection = ScannerDirection.LONG,
) -> SignalRecord:
    return SignalRecord(
        signal_id=signal_id,
        candidate_id="c" * 64,
        decision_key=decision_key,
        symbol="BTCUSDT",
        direction=direction,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        lifecycle=lifecycle,
        scanner_lifecycle=CandidateLifecycle.DETECTED,
        decision_status=decision_status,
        entry_trigger_status=trigger_status,
        selected=selected,
        ready=ready,
        rejection_reasons=list(rejection_reasons or []),
        watch_reasons=list(watch_reasons or []),
        strategy_reasons=[],
        source_snapshot_version=source_snapshot_version,
        decision_fresh=decision_fresh,
        grade=grade,
        score=92 if grade is ScannerGrade.A_PLUS else 87,
        confidence=80,
        entry_ready=entry_ready,
        entry_trigger_price=Decimal("100"),
        stop_loss_price=(
            Decimal("95")
            if direction is ScannerDirection.LONG
            else Decimal("105")
        ),
        reference_close_time=NOW - timedelta(minutes=15),
        setup_confirmed_at=NOW - timedelta(minutes=15),
        expires_at=expires_at,
        qualification_expires_at=expires_at,
        evaluated_at=NOW - timedelta(seconds=1),
        created_at=NOW - timedelta(seconds=1),
        updated_at=NOW - timedelta(seconds=1),
        universe_rank=1,
        quote_volume=Decimal("100000000"),
        spread_bps=Decimal("1"),
    )


def _assessment(
    *,
    signal: SignalRecord,
    decision: RiskDecision = RiskDecision.APPROVED,
    quantity: Decimal = Decimal("0.1"),
    notional: Decimal = Decimal("10"),
    margin: Decimal = Decimal("1"),
    updated_at: datetime = NOW,
) -> RiskAssessment:
    return RiskAssessment(
        signal_id=signal.signal_id,
        symbol=signal.symbol,
        direction=signal.direction,
        setup=signal.setup,
        setup_name=signal.setup_name,
        signal_lifecycle=signal.lifecycle,
        grade=signal.grade,
        score=signal.score,
        confidence=signal.confidence,
        decision=decision,
        blocked_reason=None if decision is RiskDecision.APPROVED else "BLOCKED",
        approved_for_execution=decision is RiskDecision.APPROVED,
        entry_trigger_price=signal.entry_trigger_price,
        stop_loss_price=signal.stop_loss_price,
        stop_distance=Decimal("5"),
        risk_percent=Decimal("1"),
        risk_budget_usdt=Decimal("0.5"),
        recommended_quantity=quantity,
        position_notional_usdt=notional,
        required_margin_usdt=margin,
        wallet_balance_usdt=Decimal("100"),
        available_balance_usdt=Decimal("90"),
        daily_realized_pnl_usdt=Decimal("0"),
        daily_unrealized_pnl_usdt=Decimal("0"),
        daily_net_pnl_usdt=Decimal("0"),
        daily_pnl_percent=Decimal("0"),
        open_position_count=0,
        current_margin_exposure_usdt=Decimal("0"),
        max_open_trades_limit=4,
        updated_at=updated_at,
        audit_codes=(
            ["RISK_APPROVED"]
            if decision is RiskDecision.APPROVED
            else ["BLOCKED"]
        ),
    )


def _snapshot(
    *,
    snapshot_id: str = "p" * 64,
    captured_at: datetime = NOW,
    position_amount: Decimal = Decimal("0"),
    leverage: int = 10,
) -> AccountSnapshot:
    return AccountSnapshot(
        snapshot_id=snapshot_id,
        captured_at=captured_at,
        source="binance_usdm_demo_private",
        source_healthy=True,
        can_trade=True,
        total_wallet_balance_usdt=Decimal("100"),
        available_balance_usdt=Decimal("90"),
        total_unrealized_pnl_usdt=Decimal("0"),
        total_initial_margin_usdt=Decimal("0"),
        balances=(),
        positions=(
            AccountPositionSnapshot(
                symbol="BTCUSDT",
                position_amount=position_amount,
                leverage=leverage,
                entry_price=Decimal("0"),
                unrealized_pnl=Decimal("0"),
            ),
        ),
        income=(),
    )


class _SignalStub:
    def __init__(self, signal: SignalRecord) -> None:
        self.signal = signal

    def get(self, signal_id: str) -> SignalRecord | None:
        return self.signal if signal_id == self.signal.signal_id else None


class _RiskStub:
    def __init__(self, assessment: RiskAssessment) -> None:
        self.assessment = assessment

    def assessments(self) -> RiskAssessmentList:
        return RiskAssessmentList(count=1, assessments=[self.assessment])


class _SnapshotStub:
    def __init__(self, snapshot: AccountSnapshot, *, fresh: bool = True) -> None:
        self.snapshot = snapshot
        self.fresh = fresh
        self.calls = 0

    def force_refresh(self) -> AccountSnapshot:
        self.calls += 1
        return self.snapshot

    def status(self) -> AccountSnapshotStatus:
        return AccountSnapshotStatus(
            cache_hits=0,
            refresh_count=self.calls,
            snapshot_age_seconds=0,
            fresh=self.fresh,
            last_successful_refresh=self.snapshot.captured_at,
            refresh_error=None,
            snapshot_id=self.snapshot.snapshot_id,
        )


@pytest.fixture
def persistence(tmp_path: Path) -> Iterator[Persistence]:
    database = Persistence(f"sqlite+pysqlite:///{tmp_path / 'phase5.db'}")
    database.initialize()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def repository(persistence: Persistence) -> ExecutionCommandRepository:
    return ExecutionCommandRepository(persistence)


def _service(
    repository: ExecutionCommandRepository,
    *,
    signal: SignalRecord | None = None,
    assessment: RiskAssessment | None = None,
    snapshot: AccountSnapshot | None = None,
    fresh: bool = True,
    enabled: bool = False,
) -> tuple[ExecutionCommandService, _SignalStub, _RiskStub, _SnapshotStub]:
    resolved_signal = signal or _signal()
    resolved_snapshot = snapshot or _snapshot()
    resolved_assessment = assessment or _assessment(signal=resolved_signal)
    signals = _SignalStub(resolved_signal)
    risk = _RiskStub(resolved_assessment)
    snapshots = _SnapshotStub(resolved_snapshot, fresh=fresh)
    service = ExecutionCommandService(
        signals,  # type: ignore[arg-type]
        risk,  # type: ignore[arg-type]
        _settings(enabled=enabled),
        snapshots,  # type: ignore[arg-type]
        repository,
        now_provider=lambda: NOW,
    )
    return service, signals, risk, snapshots


@pytest.mark.parametrize("grade", [ScannerGrade.A_PLUS, ScannerGrade.A])
def test_ready_a_grades_create_one_durable_command(
    repository: ExecutionCommandRepository,
    grade: ScannerGrade,
) -> None:
    signal = _signal(grade=grade)
    service, _, _, snapshots = _service(repository, signal=signal)

    first = service.enqueue(signal.signal_id)
    second = service.enqueue(signal.signal_id)

    assert first == second
    assert first.state is ExecutionCommandState.PENDING
    assert first.grade is grade
    assert first.approved_quantity == Decimal("0.1")
    assert first.approved_notional == Decimal("10")
    assert first.approved_margin == Decimal("1")
    assert first.leverage == 10
    assert first.take_profit_price == Decimal("110")
    assert first.entry_client_order_id == f"af-e-{signal.signal_id[:20]}"
    assert snapshots.calls == 2
    assert service.list().count == 1
    assert service.status().pending == 1
    assert service.history(first.command_id)[0].to_state is ExecutionCommandState.PENDING


@pytest.mark.parametrize(
    ("signal", "code"),
    [
        (_signal(grade=ScannerGrade.B_PLUS), "GRADE_NOT_EXECUTABLE"),
        (
            _signal(decision_status=SignalDecisionStatus.NEAR_SETUP),
            "DECISION_NOT_READY",
        ),
        (
            _signal(decision_status=SignalDecisionStatus.REJECTED),
            "DECISION_NOT_READY",
        ),
        (_signal(lifecycle=SignalLifecycle.WATCH), "SIGNAL_NOT_ACTIVE"),
        (_signal(decision_fresh=False), "DECISION_STALE"),
        (_signal(decision_key=None), "MISSING_DECISION_KEY"),
        (_signal(source_snapshot_version=None), "MISSING_SOURCE_PROVENANCE"),
        (_signal(expires_at=NOW), "EXECUTION_DECISION_EXPIRED"),
        (
            _signal(trigger_status=EntryTriggerStatus.PENDING, entry_ready=False),
            "ENTRY_TRIGGER_NOT_READY",
        ),
        (
            _signal(watch_reasons=["ENTRY_NOT_READY"]),
            "DECISION_HAS_BLOCKING_OR_WATCH_REASONS",
        ),
        (
            _signal(rejection_reasons=["BLOCKED"]),
            "DECISION_HAS_BLOCKING_OR_WATCH_REASONS",
        ),
    ],
)
def test_ineligible_signal_never_creates_command(
    repository: ExecutionCommandRepository,
    signal: SignalRecord,
    code: str,
) -> None:
    service, _, _, _ = _service(repository, signal=signal)
    with pytest.raises(AppError) as exc:
        service.enqueue(signal.signal_id)
    assert exc.value.code == code
    assert service.list().count == 0


def test_stale_account_and_risk_fail_closed(
    repository: ExecutionCommandRepository,
) -> None:
    signal = _signal()
    stale_risk = _assessment(signal=signal, updated_at=NOW - timedelta(seconds=1))
    service, _, _, _ = _service(
        repository,
        signal=signal,
        assessment=stale_risk,
    )
    with pytest.raises(AppError) as risk_error:
        service.enqueue(signal.signal_id)
    assert risk_error.value.code == "RISK_ASSESSMENT_STALE"

    service, _, _, _ = _service(repository, signal=signal, fresh=False)
    with pytest.raises(AppError) as snapshot_error:
        service.enqueue(signal.signal_id)
    assert snapshot_error.value.code == "ACCOUNT_SNAPSHOT_STALE"


def test_rejected_risk_and_conflicting_plan_fail_closed(
    repository: ExecutionCommandRepository,
) -> None:
    signal = _signal()
    rejected = _assessment(signal=signal, decision=RiskDecision.BLOCKED)
    service, _, _, _ = _service(repository, signal=signal, assessment=rejected)
    with pytest.raises(AppError) as rejected_error:
        service.enqueue(signal.signal_id)
    assert rejected_error.value.code == "RISK_NOT_APPROVED"

    invalid_notional = _assessment(signal=signal, notional=Decimal("11"))
    service, _, _, _ = _service(
        repository,
        signal=signal,
        assessment=invalid_notional,
    )
    with pytest.raises(AppError) as notional_error:
        service.enqueue(signal.signal_id)
    assert notional_error.value.code == "APPROVED_NOTIONAL_CONFLICT"

    wrong_direction_signal = _signal(direction=ScannerDirection.SHORT)
    wrong_direction_assessment = _assessment(signal=wrong_direction_signal).model_copy(
        update={"direction": ScannerDirection.LONG}
    )
    service, _, _, _ = _service(
        repository,
        signal=wrong_direction_signal,
        assessment=wrong_direction_assessment,
    )
    with pytest.raises(AppError) as direction_error:
        service.enqueue(wrong_direction_signal.signal_id)
    assert direction_error.value.code == "EXECUTION_DIRECTION_CONFLICT"


def test_missing_persistence_never_falls_back_to_memory() -> None:
    signal = _signal()
    service = ExecutionCommandService(
        _SignalStub(signal),  # type: ignore[arg-type]
        _RiskStub(_assessment(signal=signal)),  # type: ignore[arg-type]
        _settings(),
        _SnapshotStub(_snapshot()),  # type: ignore[arg-type]
        None,
        now_provider=lambda: NOW,
    )
    with pytest.raises(AppError) as exc:
        service.enqueue(signal.signal_id)
    assert exc.value.code == "EXECUTION_COMMAND_PERSISTENCE_REQUIRED"
    assert service.status().persistence_available is False


def test_new_decision_snapshot_creates_new_identity(
    repository: ExecutionCommandRepository,
) -> None:
    first_signal = _signal(signal_id="a" * 64)
    first_service, _, _, _ = _service(repository, signal=first_signal)
    first = first_service.enqueue(first_signal.signal_id)

    second_signal = _signal(
        signal_id="b" * 64,
        decision_key="e" * 64,
        source_snapshot_version="n" * 64,
    )
    second_service, _, _, _ = _service(repository, signal=second_signal)
    second = second_service.enqueue(second_signal.signal_id)

    assert first.command_id != second.command_id
    assert second_service.list().count == 2


def test_concurrent_claim_allows_only_one_worker(
    repository: ExecutionCommandRepository,
) -> None:
    service, _, _, _ = _service(repository)
    command = service.enqueue(SIGNAL_ID)

    def claim(worker: str) -> str | None:
        claimed = repository.claim_next(worker_id=worker, now=NOW)
        return claimed.command_id if claimed is not None else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["worker-1", "worker-2"]))

    assert results.count(command.command_id) == 1
    assert sum(result is not None for result in results) == 1
    claimed = service.get(command.command_id)
    assert claimed is not None
    assert claimed.state is ExecutionCommandState.CLAIMED


def test_illegal_transition_and_stale_claim_recovery(
    repository: ExecutionCommandRepository,
) -> None:
    service, _, _, _ = _service(repository)
    command = service.enqueue(SIGNAL_ID)
    with pytest.raises(IllegalExecutionCommandTransition):
        repository.transition(
            command.command_id,
            ExecutionCommandState.COMPLETED,
            reason="ILLEGAL",
            changed_at=NOW,
        )

    first = repository.claim_next(worker_id="worker-1", now=NOW)
    assert first is not None
    reclaimed = repository.claim_next(
        worker_id="worker-2",
        now=NOW + timedelta(seconds=31),
    )
    assert reclaimed is not None
    assert reclaimed.worker_id == "worker-2"
    assert any(
        item.reason == "STALE_WORKER_CLAIM"
        for item in repository.history(command.command_id)
    )


class _Lease:
    def __init__(self, *, valid: bool = True) -> None:
        self.valid = valid
        self.calls = 0

    def require_valid(self) -> None:
        self.calls += 1
        if not self.valid:
            raise ExecutionLeaderLost("lost")


class _Backend:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def activate(self, signal_id: str) -> DemoTradeRecord:
        self.calls.append(signal_id)
        if self.error is not None:
            raise self.error
        return _trade(signal_id)


def _trade(signal_id: str = SIGNAL_ID) -> DemoTradeRecord:
    return DemoTradeRecord(
        trade_id="t" * 64,
        signal_id=signal_id,
        symbol="BTCUSDT",
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        lifecycle=DemoTradeLifecycle.OPEN,
        protection_state=DemoProtectionState.PROTECTED,
        grade=ScannerGrade.A_PLUS,
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("95"),
        take_profit_price=Decimal("110"),
        exchange_order_id="entry-1",
        client_order_id=f"af-e-{signal_id[:20]}",
        stop_order_id="stop-1",
        stop_client_order_id=f"af-s-{signal_id[:20]}",
        take_profit_order_id="take-1",
        take_profit_client_order_id=f"af-t-{signal_id[:20]}",
        requested_quantity=Decimal("0.1"),
        executed_quantity=Decimal("0.1"),
        order_status="FILLED",
        tracked_margin_usdt=Decimal("1"),
        opened_at=NOW,
        updated_at=NOW,
    )


def _ready_gate() -> AutomationRecoveryGate:
    gate = AutomationRecoveryGate()
    gate.begin()
    gate.mark_exchange_reconciled()
    gate.mark_signals_revalidated()
    gate.mark_ready()
    return gate


def test_worker_disabled_makes_zero_backend_calls(
    repository: ExecutionCommandRepository,
) -> None:
    service, _, _, _ = _service(repository, enabled=False)
    service.enqueue(SIGNAL_ID)
    backend = _Backend()
    worker = DemoExecutionWorker(
        service,
        backend,  # type: ignore[arg-type]
        _settings(enabled=False),
        _ready_gate(),
        _Lease(),
        now_provider=lambda: NOW,
    )

    assert worker.process_one() == 0
    assert backend.calls == []
    assert service.status().pending == 1
    assert worker.status().execution_enabled is False


def test_worker_completes_only_verified_protected_trade(
    repository: ExecutionCommandRepository,
) -> None:
    service, _, _, _ = _service(repository, enabled=True)
    command = service.enqueue(SIGNAL_ID)
    backend = _Backend()
    lease = _Lease()
    worker = DemoExecutionWorker(
        service,
        backend,  # type: ignore[arg-type]
        _settings(enabled=True),
        _ready_gate(),
        lease,
        now_provider=lambda: NOW,
    )

    assert worker.process_one() == 1
    completed = service.get(command.command_id)
    assert completed is not None
    assert completed.state is ExecutionCommandState.COMPLETED
    assert completed.entry_exchange_order_id == "entry-1"
    assert completed.stop_exchange_order_id == "stop-1"
    assert completed.take_profit_exchange_order_id == "take-1"
    assert backend.calls == [SIGNAL_ID]
    assert lease.calls == 2
    assert [item.to_state for item in service.history(command.command_id)][-4:] == [
        ExecutionCommandState.ENTRY_CONFIRMED,
        ExecutionCommandState.PROTECTION_PENDING,
        ExecutionCommandState.PROTECTED,
        ExecutionCommandState.COMPLETED,
    ]


@pytest.mark.parametrize(
    "code",
    [
        "ENTRY_FILL_NOT_VERIFIED",
        "PROTECTIVE_ORDER_FAILED_POSITION_CLOSED",
        "UNPROTECTED_DEMO_POSITION",
    ],
)
def test_ambiguous_or_partial_exchange_result_requires_recovery(
    repository: ExecutionCommandRepository,
    code: str,
) -> None:
    service, _, _, _ = _service(repository, enabled=True)
    command = service.enqueue(SIGNAL_ID)
    backend = _Backend(error=AppError(status_code=502, code=code, message=code))
    worker = DemoExecutionWorker(
        service,
        backend,  # type: ignore[arg-type]
        _settings(enabled=True),
        _ready_gate(),
        _Lease(),
        now_provider=lambda: NOW,
    )

    assert worker.process_one() == 0
    failed = service.get(command.command_id)
    assert failed is not None
    assert failed.state is ExecutionCommandState.RECOVERY_REQUIRED
    assert failed.failure_reason == code


def test_worker_revalidates_and_blocks_superseded_signal_without_submission(
    repository: ExecutionCommandRepository,
) -> None:
    service, signals, _, _ = _service(repository, enabled=True)
    command = service.enqueue(SIGNAL_ID)
    signals.signal = signals.signal.model_copy(update={"decision_fresh": False})
    backend = _Backend()
    worker = DemoExecutionWorker(
        service,
        backend,  # type: ignore[arg-type]
        _settings(enabled=True),
        _ready_gate(),
        _Lease(),
        now_provider=lambda: NOW,
    )

    assert worker.process_one() == 0
    blocked = service.get(command.command_id)
    assert blocked is not None
    assert blocked.state is ExecutionCommandState.BLOCKED
    assert blocked.failure_reason == "DECISION_STALE"
    assert backend.calls == []


def test_worker_requires_recovery_and_leader_before_claim(
    repository: ExecutionCommandRepository,
) -> None:
    service, _, _, _ = _service(repository, enabled=True)
    service.enqueue(SIGNAL_ID)
    backend = _Backend()
    gate = AutomationRecoveryGate()
    worker = DemoExecutionWorker(
        service,
        backend,  # type: ignore[arg-type]
        _settings(enabled=True),
        gate,
        _Lease(),
        now_provider=lambda: NOW,
    )
    assert worker.process_one() == 0
    assert backend.calls == []

    gate = _ready_gate()
    worker = DemoExecutionWorker(
        service,
        backend,  # type: ignore[arg-type]
        _settings(enabled=True),
        gate,
        _Lease(valid=False),
        now_provider=lambda: NOW,
    )
    assert worker.process_one() == 0
    assert gate.snapshot().automation_ready is False
    assert backend.calls == []


def test_migration_and_openapi_contract_are_additive(
    persistence: Persistence,
) -> None:
    tables = set(sqlalchemy_inspect(persistence.engine).get_table_names())
    assert "execution_commands" in tables
    assert "execution_command_transitions" in tables
    assert "signals" in tables
    assert "trades" in tables

    application = create_app(_settings(enabled=False))
    schema = application.openapi()
    activation = schema["paths"]["/api/v1/execution/demo/activate/{signal_id}"]["post"]
    response_schema = activation["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert response_schema["$ref"].endswith("ExecutionCommand")
    assert "/api/v1/execution/demo/commands" in schema["paths"]
    assert "/api/v1/execution/demo/worker/status" in schema["paths"]


def test_architecture_keeps_private_execution_outside_decision_and_api() -> None:
    route_source = inspect.getsource(execution_routes.execution_activate)
    decision_source = inspect.getsource(SignalDecisionEngine)
    main_source = inspect.getsource(create_app)

    assert "get_execution_command_service" in route_source
    assert "service.enqueue" in route_source
    assert ".activate(" not in route_source
    assert "Binance" not in decision_source
    assert "get_execution_worker" in main_source
    assert "auto_execute_pending" not in main_source


def test_default_execution_setting_remains_false() -> None:
    assert Settings(_env_file=None, environment="test").execution_enabled is False
