"""Direct branch coverage for Phase 5 command and single-worker safety."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast, List

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1.routes.execution import (
    execution_command_detail,
    execution_command_history,
    execution_command_status,
    execution_commands,
    execution_worker_status,
)
from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.persistence.database import Persistence
from app.persistence.execution_command_repository import ExecutionCommandRepository
from app.schemas.execution import (
    DemoProtectionState,
    DemoTradeLifecycle,
    DemoTradeRecord,
)
from app.schemas.execution_command import (
    ExecutionCommand,
    ExecutionCommandList,
    ExecutionCommandState,
    ExecutionCommandStatus,
    ExecutionCommandTransition,
    ExecutionWorkerStatus,
)
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.services.account_snapshot import AccountSnapshot
from app.services.execution_command import ExecutionCommandService
from app.services.execution_facade import WorkerIsolatedExecutionService
from app.services.execution_leader_safety import ExecutionLeaderLost
from app.services.execution_private_adapter import QueryBeforeRetrySnapshotPrivateClient
from app.services.execution_worker import DemoExecutionWorker
from app.services.recovery import AutomationRecoveryGate

NOW = datetime(2026, 7, 19, 13, 0, tzinfo=UTC)


def _settings(*, enabled: bool = False, multiple: Decimal = Decimal("2")) -> Settings:
    values: dict[str, Any] = {}
    if enabled:
        values.update(
            binance_demo_base_url="https://demo-fapi.binance.com",
            binance_demo_api_key="coverage-key",
            binance_demo_api_secret="coverage-secret",
        )
    return Settings(
        _env_file=None,
        environment="test",
        scanner_auto_start=False,
        execution_enabled=enabled,
        execution_take_profit_r_multiple=multiple,
        **values,
    )


def _command(
    *,
    direction: ScannerDirection = ScannerDirection.LONG,
    created_at: datetime = NOW,
    expires_at: datetime = NOW + timedelta(minutes=10),
) -> ExecutionCommand:
    entry = Decimal("100")
    stop = Decimal("95") if direction is ScannerDirection.LONG else Decimal("105")
    target = Decimal("110") if direction is ScannerDirection.LONG else Decimal("90")
    return ExecutionCommand(
        command_id="c" * 64,
        idempotency_key="i" * 64,
        deterministic_execution_identity="x" * 64,
        signal_id="s" * 64,
        decision_key="d" * 64,
        risk_decision_id="r" * 64,
        symbol="BTCUSDT",
        direction=direction,
        setup=ScannerSetup.TREND_PULLBACK,
        grade=ScannerGrade.A_PLUS,
        source_snapshot_version="v" * 64,
        entry_trigger_price=entry,
        stop_loss_price=stop,
        take_profit_price=target,
        take_profit_r_multiple=Decimal("2"),
        approved_quantity=Decimal("0.1"),
        approved_notional=Decimal("10"),
        approved_margin=Decimal("1"),
        leverage=10,
        account_snapshot_id="a" * 64,
        state=ExecutionCommandState.PENDING,
        created_at=created_at,
        updated_at=created_at,
        expires_at=expires_at,
        entry_client_order_id="af-e-stable",
        stop_client_order_id="af-s-stable",
        take_profit_client_order_id="af-t-stable",
    )


def _trade(command: ExecutionCommand) -> DemoTradeRecord:
    return DemoTradeRecord(
        trade_id="t" * 64,
        signal_id=command.signal_id,
        symbol=command.symbol,
        direction=command.direction,
        setup=command.setup,
        setup_name="Trend Pullback",
        lifecycle=DemoTradeLifecycle.OPEN,
        protection_state=DemoProtectionState.PROTECTED,
        grade=command.grade,
        entry_price=command.entry_trigger_price,
        stop_loss_price=command.stop_loss_price,
        take_profit_price=command.take_profit_price,
        exchange_order_id="entry-exchange",
        client_order_id=command.entry_client_order_id,
        stop_order_id="stop-exchange",
        stop_client_order_id=command.stop_client_order_id,
        take_profit_order_id="take-exchange",
        take_profit_client_order_id=command.take_profit_client_order_id,
        requested_quantity=command.approved_quantity,
        executed_quantity=command.approved_quantity,
        order_status="FILLED",
        tracked_margin_usdt=command.approved_margin,
        opened_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"signal_id": "z" * 64}, "EXCHANGE_SIGNAL_IDENTITY_CONFLICT"),
        ({"symbol": "ETHUSDT"}, "EXCHANGE_TRADE_IDENTITY_CONFLICT"),
        ({"direction": ScannerDirection.SHORT}, "EXCHANGE_TRADE_IDENTITY_CONFLICT"),
        ({"client_order_id": "wrong-entry"}, "ENTRY_CLIENT_ORDER_ID_CONFLICT"),
        ({"stop_client_order_id": "wrong-stop"}, "STOP_CLIENT_ORDER_ID_CONFLICT"),
        ({"take_profit_client_order_id": "wrong-take"}, "TAKE_PROFIT_CLIENT_ORDER_ID_CONFLICT"),
        ({"order_status": "NEW"}, "ENTRY_FILL_NOT_VERIFIED"),
        ({"executed_quantity": Decimal("0")}, "ENTRY_EXECUTED_QUANTITY_INVALID"),
        ({"executed_quantity": Decimal("0.2")}, "ENTRY_QUANTITY_EXCEEDS_APPROVAL"),
        ({"stop_order_id": None}, "PROTECTION_IDENTITY_MISSING"),
        ({"take_profit_order_id": None}, "PROTECTION_IDENTITY_MISSING"),
        ({"take_profit_price": Decimal("109")}, "TAKE_PROFIT_R_MULTIPLE_CONFLICT"),
    ],
)
def test_worker_rejects_every_conflicting_exchange_result(
    updates: dict[str, Any],
    code: str,
) -> None:
    command = _command()
    trade = _trade(command).model_copy(update=updates)
    with pytest.raises(AppError) as exc:
        DemoExecutionWorker._validate_trade(command, trade)
    assert exc.value.code == code


def test_worker_accepts_short_protected_result() -> None:
    command = _command(direction=ScannerDirection.SHORT)
    DemoExecutionWorker._validate_trade(command, _trade(command))


@pytest.mark.parametrize(
    "updates",
    [
        {"grade": ScannerGrade.B_PLUS},
        {"stop_loss_price": Decimal("100")},
        {"take_profit_price": Decimal("100")},
        {"expires_at": NOW},
    ],
)
def test_command_contract_rejects_invalid_long_state(updates: dict[str, Any]) -> None:
    payload = _command().model_dump()
    payload.update(updates)
    with pytest.raises(ValidationError):
        ExecutionCommand(**payload)


def test_command_contract_rejects_invalid_short_levels() -> None:
    payload = _command(direction=ScannerDirection.SHORT).model_dump()
    payload["stop_loss_price"] = Decimal("100")
    with pytest.raises(ValidationError):
        ExecutionCommand(**payload)
    payload = _command(direction=ScannerDirection.SHORT).model_dump()
    payload["take_profit_price"] = Decimal("100")
    with pytest.raises(ValidationError):
        ExecutionCommand(**payload)


@pytest.fixture
def persistence(tmp_path: Path) -> Iterator[Persistence]:
    value = Persistence(f"sqlite+pysqlite:///{tmp_path / 'coverage.db'}")
    value.initialize()
    try:
        yield value
    finally:
        value.close()


def test_repository_missing_same_state_expiry_and_timezone_paths(
    persistence: Persistence,
) -> None:
    repository = ExecutionCommandRepository(persistence)
    assert repository.available is True
    assert repository.get("missing") is None
    with pytest.raises(KeyError):
        repository.transition(
            "missing",
            ExecutionCommandState.BLOCKED,
            reason="MISSING",
            changed_at=NOW,
        )

    command = repository.create(_command())
    same = repository.transition(
        command.command_id,
        ExecutionCommandState.PENDING,
        reason="SAME",
        changed_at=NOW,
    )
    assert same.state is ExecutionCommandState.PENDING
    repository.transition(
        command.command_id,
        ExecutionCommandState.BLOCKED,
        reason="TEST_TERMINAL",
        changed_at=NOW,
    )

    expired_repository = ExecutionCommandRepository(persistence)
    expired = _command(
        created_at=NOW - timedelta(minutes=2),
        expires_at=NOW - timedelta(minutes=1),
    ).model_copy(
        update={
            "command_id": "e" * 64,
            "idempotency_key": "f" * 64,
            "deterministic_execution_identity": "g" * 64,
            "signal_id": "u" * 64,
            "decision_key": "w" * 64,
            "source_snapshot_version": "h" * 64,
        }
    )
    expired_repository.create(expired)
    assert expired_repository.claim_next(worker_id="worker", now=NOW) is None
    stored = expired_repository.get(expired.command_id)
    assert stored is not None
    assert stored.state is ExecutionCommandState.EXPIRED

    naive = _command().model_copy(
        update={
            "command_id": "n" * 64,
            "idempotency_key": "m" * 64,
            "deterministic_execution_identity": "l" * 64,
            "created_at": NOW.replace(tzinfo=None),
        }
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        repository.create(naive)


class _Queue:
    def __init__(
        self,
        *,
        command: ExecutionCommand | None = None,
        persistence_available: bool = True,
        revalidate_error: AppError | None = None,
        transition_error: Exception | None = None,
    ) -> None:
        self.command = command
        self.persistence_available = persistence_available
        self.revalidate_error = revalidate_error
        self.transition_error = transition_error
        self.transitions: list[tuple[ExecutionCommandState, str]] = []

    def claim_next(self, worker_id: str) -> ExecutionCommand | None:
        return self.command

    def revalidate(self, command: ExecutionCommand) -> tuple[object, object, object]:
        if self.revalidate_error is not None:
            raise self.revalidate_error
        return object(), object(), object()

    def transition(
        self,
        command_id: str,
        state: ExecutionCommandState,
        *,
        reason: str,
        updates: dict[str, Any] | None = None,
    ) -> ExecutionCommand:
        if self.transition_error is not None:
            raise self.transition_error
        self.transitions.append((state, reason))
        assert self.command is not None
        return self.command.model_copy(update={"state": state})


class _Backend:
    def __init__(self, result: DemoTradeRecord | Exception) -> None:
        self.result = result
        self.calls = 0

    def activate(self, signal_id: str) -> DemoTradeRecord:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Lease:
    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes = outcomes

    def require_valid(self) -> None:
        valid = self.outcomes.pop(0) if self.outcomes else True
        if not valid:
            raise ExecutionLeaderLost("lost")


def _ready_gate() -> AutomationRecoveryGate:
    gate = AutomationRecoveryGate()
    gate.begin()
    gate.mark_exchange_reconciled()
    gate.mark_signals_revalidated()
    gate.mark_ready()
    return gate


def _worker(
    queue: _Queue,
    backend: _Backend,
    gate: AutomationRecoveryGate,
    lease: _Lease,
) -> DemoExecutionWorker:
    return DemoExecutionWorker(
        queue,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        _settings(enabled=True),
        gate,
        lease,
        now_provider=lambda: NOW,
    )


def test_worker_status_and_early_fail_closed_paths() -> None:
    gate = _ready_gate()
    valid = _worker(_Queue(command=None), _Backend(RuntimeError()), gate, _Lease([True]))
    assert valid.status().leader_valid is True
    assert valid.process_one() == 0
    assert valid.status().last_result == "NO_PENDING_COMMAND"

    invalid_status = _worker(
        _Queue(command=None),
        _Backend(RuntimeError()),
        gate,
        _Lease([False]),
    )
    assert invalid_status.status().leader_valid is False

    unavailable = _worker(
        _Queue(command=None, persistence_available=False),
        _Backend(RuntimeError()),
        gate,
        _Lease([]),
    )
    assert unavailable.process_one() == 0
    assert unavailable.status().last_result == "PERSISTENCE_REQUIRED"


def test_worker_revalidation_second_lease_and_unexpected_paths() -> None:
    command = _command()
    gate = _ready_gate()
    blocked_queue = _Queue(
        command=command,
        revalidate_error=AppError(status_code=409, code="DECISION_STALE", message="stale"),
    )
    blocked = _worker(blocked_queue, _Backend(_trade(command)), gate, _Lease([True]))
    assert blocked.process_one() == 0
    assert blocked_queue.transitions[-1] == (
        ExecutionCommandState.BLOCKED,
        "DECISION_STALE",
    )

    gate = _ready_gate()
    lost_queue = _Queue(command=command)
    lost = _worker(lost_queue, _Backend(_trade(command)), gate, _Lease([True, False]))
    assert lost.process_one() == 0
    assert lost_queue.transitions[-1][0] is ExecutionCommandState.RECOVERY_REQUIRED
    assert gate.snapshot().automation_ready is False

    gate = _ready_gate()
    unexpected_queue = _Queue(command=command)
    unexpected = _worker(
        unexpected_queue,
        _Backend(RuntimeError("unexpected")),
        gate,
        _Lease([True, True]),
    )
    assert unexpected.process_one() == 0
    assert unexpected_queue.transitions[-1] == (
        ExecutionCommandState.RECOVERY_REQUIRED,
        "WORKER_UNEXPECTED_FAILURE",
    )


def test_worker_persistence_failure_fails_recovery_gate() -> None:
    command = _command()
    gate = _ready_gate()
    queue = _Queue(
        command=command,
        transition_error=RuntimeError("database unavailable"),
    )
    worker = _worker(
        queue,
        _Backend(AppError(status_code=502, code="AMBIGUOUS", message="ambiguous")),
        gate,
        _Lease([True, True]),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        worker.process_one()
    assert gate.snapshot().automation_ready is False


def test_command_service_private_validation_helpers() -> None:
    service = object.__new__(ExecutionCommandService)
    service._settings = _settings()
    service._now = lambda: NOW

    assert service._take_profit(
        direction=ScannerDirection.LONG,
        entry=Decimal("100"),
        stop=Decimal("95"),
    ) == Decimal("110")
    assert service._take_profit(
        direction=ScannerDirection.SHORT,
        entry=Decimal("100"),
        stop=Decimal("105"),
    ) == Decimal("90")
    assert service.client_order_ids("q" * 64) == (
        "af-e-" + "q" * 20,
        "af-s-" + "q" * 20,
        "af-t-" + "q" * 20,
    )
    assert len(service._digest({"a": 1})) == 64
    with pytest.raises(AppError):
        service._required_text(None, "MISSING")
    with pytest.raises(AppError):
        service._required_decimal(Decimal("0"), "INVALID")
    with pytest.raises(AppError):
        service._required_grade(ScannerGrade.B_PLUS)
    with pytest.raises(AppError):
        service._leverage(
            AccountSnapshot(
                snapshot_id="a" * 64,
                captured_at=NOW,
                source="binance_usdm_demo_private",
                source_healthy=True,
                can_trade=True,
                total_wallet_balance_usdt=Decimal("1"),
                available_balance_usdt=Decimal("1"),
                total_unrealized_pnl_usdt=Decimal("0"),
                total_initial_margin_usdt=Decimal("0"),
                balances=(),
                positions=(),
                income=(),
            ),
            "BTCUSDT",
        )

    service._settings = _settings(multiple=Decimal("0"))
    with pytest.raises(AppError):
        service._take_profit(
            direction=ScannerDirection.LONG,
            entry=Decimal("100"),
            stop=Decimal("95"),
        )
    service._settings = _settings()
    with pytest.raises(AppError):
        service._take_profit(
            direction=ScannerDirection.LONG,
            entry=Decimal("100"),
            stop=Decimal("100"),
        )


class _FacadeInner:
    marker = "delegated"

    def status(self) -> Any:
        return "status"

    def plans(self) -> Any:
        return "plans"

    def trades(self) -> Any:
        return "trades"

    def account(self) -> Any:
        return "account"

    def get_trade(self, trade_id: str) -> Any:
        return trade_id

    def store_trade(self, trade: Any) -> Any:
        return trade


class _FacadeCommands:
    def enqueue_all_ready(self) -> int:
        return 3


def test_facade_delegates_only_read_and_storage_methods() -> None:
    facade: Any = WorkerIsolatedExecutionService(
        cast(Any, _FacadeInner()),
        cast(Any, _FacadeCommands()),
    )
    assert facade.status() == "status"
    assert facade.plans() == "plans"
    assert facade.trades() == "trades"
    assert facade.account() == "account"
    assert facade.get_trade("trade") == "trade"
    marker = object()
    assert facade.store_trade(cast(Any, marker)) is marker
    assert facade.marker == "delegated"


class _RouteService:
    def __init__(self, command: ExecutionCommand | None) -> None:
        self.command = command

    def status(self) -> ExecutionCommandStatus:
        return ExecutionCommandStatus(
            execution_enabled=False,
            persistence_available=True,
            pending=1,
            claimed=0,
            recovery_required=0,
            completed=0,
            blocked=0,
            failed=0,
            expired=0,
        )

    def list(self) -> ExecutionCommandList:
        commands = [self.command] if self.command is not None else []
        return ExecutionCommandList(count=len(commands), commands=commands)

    def get(self, command_id: str) -> ExecutionCommand | None:
        return self.command

    def history(self, command_id: str) -> List[ExecutionCommandTransition]:
        return [
            ExecutionCommandTransition(
                sequence=1,
                from_state=None,
                to_state=ExecutionCommandState.PENDING,
                reason="COMMAND_CREATED",
                changed_at=NOW,
            )
        ]


class _RouteWorker:
    def status(self) -> ExecutionWorkerStatus:
        return ExecutionWorkerStatus(
            worker_id="worker",
            execution_enabled=False,
            recovery_ready=False,
            leader_valid=False,
            persistence_available=True,
        )


@pytest.mark.anyio
async def test_command_observability_routes_cover_success_and_missing() -> None:
    command = _command()
    service = _RouteService(command)
    assert (await execution_command_status(service)).pending == 1  # type: ignore[arg-type]
    assert (await execution_commands(service)).count == 1  # type: ignore[arg-type]
    assert (
        await execution_command_detail(command.command_id, service)  # type: ignore[arg-type]
    ) == command
    assert (
        len(
            await execution_command_history(command.command_id, service)  # type: ignore[arg-type]
        )
        == 1
    )
    assert (await execution_worker_status(_RouteWorker())).worker_id == "worker"  # type: ignore[arg-type]

    missing = _RouteService(None)
    with pytest.raises(HTTPException) as detail_error:
        await execution_command_detail("m" * 64, missing)  # type: ignore[arg-type]
    assert detail_error.value.status_code == 404
    with pytest.raises(HTTPException) as history_error:
        await execution_command_history("m" * 64, missing)  # type: ignore[arg-type]
    assert history_error.value.status_code == 404


class _PrivateRaw:
    def __init__(self, error: BinanceDemoPrivateClientError) -> None:
        self.error = error

    def query_algo_order(self, **kwargs: Any) -> dict[str, Any]:
        raise self.error


class _Snapshots:
    pass


def test_private_adapter_propagates_non_not_found_query_error() -> None:
    error = BinanceDemoPrivateClientError(
        "unavailable",
        status_code=503,
        exchange_code=-1000,
    )
    adapter = QueryBeforeRetrySnapshotPrivateClient(
        cast(Any, _PrivateRaw(error)),
        cast(Any, _Snapshots()),
    )
    with pytest.raises(BinanceDemoPrivateClientError) as exc:
        adapter._existing_algo_order(symbol="BTCUSDT", client_order_id="id")
    assert exc.value is error
