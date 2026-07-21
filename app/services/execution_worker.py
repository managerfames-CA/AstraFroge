"""Single Phase 5 worker that exclusively owns new Binance Demo entry submission."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from app.core.config import Settings
from app.core.errors import AppError
from app.schemas.execution import DemoTradeRecord
from app.schemas.execution_command import (
    ExecutionCommand,
    ExecutionCommandState,
    ExecutionWorkerStatus,
)
from app.services.execution import DemoExecutionService
from app.services.execution_command import ExecutionCommandService
from app.services.execution_leader_safety import ExecutionLeaderLost
from app.services.recovery import AutomationRecoveryGate


class ExecutionWorkerLeaderLease(Protocol):
    def require_valid(self) -> None: ...


_BLOCKED_PRE_SUBMISSION_CODES = frozenset(
    {
        "ACCOUNT_NOT_EXECUTABLE",
        "ACCOUNT_SNAPSHOT_REFRESH_FAILED",
        "ACCOUNT_SNAPSHOT_STALE",
        "APPROVED_MARGIN_CONFLICT",
        "APPROVED_NOTIONAL_CONFLICT",
        "APPROVED_QUANTITY_CONFLICT",
        "DECISION_KEY_SUPERSEDED",
        "DECISION_NOT_READY",
        "DECISION_STALE",
        "ENTRY_TRIGGER_CONFLICT",
        "ENTRY_TRIGGER_NOT_READY",
        "EXECUTION_COMMAND_EXPIRED",
        "EXECUTION_DECISION_EXPIRED",
        "EXECUTION_DIRECTION_CONFLICT",
        "EXISTING_EXCHANGE_POSITION_CONFLICT",
        "GRADE_NOT_EXECUTABLE",
        "LEVERAGE_CONFLICT",
        "MISSING_DECISION_KEY",
        "MISSING_SOURCE_PROVENANCE",
        "PLAN_NOT_EXECUTABLE",
        "RISK_ASSESSMENT_MISSING",
        "RISK_ASSESSMENT_STALE",
        "RISK_NOT_APPROVED",
        "SIGNAL_NOT_ACTIVE",
        "SIGNAL_NOT_FOUND",
        "SOURCE_SNAPSHOT_SUPERSEDED",
        "STOP_LOSS_CONFLICT",
        "TAKE_PROFIT_CONFLICT",
    }
)


class DemoExecutionWorker:
    """Claim one durable command, revalidate, and invoke the worker-owned order backend."""

    def __init__(
        self,
        commands: ExecutionCommandService,
        order_backend: DemoExecutionService,
        settings: Settings,
        recovery_gate: AutomationRecoveryGate,
        leader_lease: ExecutionWorkerLeaderLease,
        *,
        worker_id: str = "astraforge-demo-execution-worker",
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._commands = commands
        self._backend = order_backend
        self._settings = settings
        self._gate = recovery_gate
        self._lease = leader_lease
        self._worker_id = worker_id
        self._now = now_provider or (lambda: datetime.now(UTC))
        self._last_cycle_at: datetime | None = None
        self._last_command_id: str | None = None
        self._last_result: str | None = None

    def status(self) -> ExecutionWorkerStatus:
        recovery_ready = self._gate.snapshot().automation_ready
        leader_valid = False
        if self._settings.execution_enabled and recovery_ready:
            try:
                self._lease.require_valid()
                leader_valid = True
            except ExecutionLeaderLost:
                leader_valid = False
        return ExecutionWorkerStatus(
            worker_id=self._worker_id,
            execution_enabled=self._settings.execution_enabled,
            recovery_ready=recovery_ready,
            leader_valid=leader_valid,
            persistence_available=self._commands.persistence_available,
            last_cycle_at=self._last_cycle_at,
            last_command_id=self._last_command_id,
            last_result=self._last_result,
        )

    def process_one(self) -> int:
        """Process at most one command; disabled mode performs no exchange mutation."""

        self._last_cycle_at = self._now().astimezone(UTC)
        if not self._settings.execution_enabled:
            self._last_result = "EXECUTION_DISABLED"
            return 0
        if not self._commands.persistence_available:
            self._last_result = "PERSISTENCE_REQUIRED"
            return 0
        if not self._gate.snapshot().automation_ready:
            self._last_result = "RECOVERY_NOT_COMPLETE"
            return 0
        try:
            self._lease.require_valid()
        except ExecutionLeaderLost:
            self._gate.fail("EXECUTION_LEADER_LOST")
            self._last_result = "EXECUTION_LEADER_LOST"
            return 0

        command = self._commands.claim_next(self._worker_id)
        if command is None:
            self._last_result = "NO_PENDING_COMMAND"
            return 0
        self._last_command_id = command.command_id

        try:
            self._commands.revalidate(command)
        except AppError as exc:
            self._commands.transition(
                command.command_id,
                ExecutionCommandState.BLOCKED,
                reason=exc.code,
                updates={"failure_reason": exc.code},
            )
            self._last_result = exc.code
            return 0

        try:
            self._lease.require_valid()
        except ExecutionLeaderLost:
            self._gate.fail("EXECUTION_LEADER_LOST")
            self._commands.transition(
                command.command_id,
                ExecutionCommandState.RECOVERY_REQUIRED,
                reason="EXECUTION_LEADER_LOST",
                updates={"failure_reason": "EXECUTION_LEADER_LOST"},
            )
            self._last_result = "EXECUTION_LEADER_LOST"
            return 0

        try:
            self._commands.transition(
                command.command_id,
                ExecutionCommandState.SUBMITTING,
                reason="WORKER_SUBMISSION_STARTED",
            )
        except Exception:
            self._gate.fail("EXECUTION_COMMAND_PERSISTENCE_FAILED")
            self._last_result = "EXECUTION_COMMAND_PERSISTENCE_FAILED"
            raise

        try:
            trade = self._backend.activate(command.signal_id)
            self._validate_trade(command, trade)
            self._commands.transition(
                command.command_id,
                ExecutionCommandState.ENTRY_CONFIRMED,
                reason="ENTRY_FILL_VERIFIED",
                updates={
                    "entry_exchange_order_id": trade.exchange_order_id,
                    "executed_quantity": trade.executed_quantity,
                    "average_entry_price": trade.entry_price,
                },
            )
            self._commands.transition(
                command.command_id,
                ExecutionCommandState.PROTECTION_PENDING,
                reason="PROTECTION_VERIFICATION_STARTED",
            )
            self._commands.transition(
                command.command_id,
                ExecutionCommandState.PROTECTED,
                reason="STOP_AND_TAKE_PROFIT_VERIFIED",
                updates={
                    "stop_exchange_order_id": trade.stop_order_id,
                    "take_profit_exchange_order_id": trade.take_profit_order_id,
                },
            )
            self._commands.transition(
                command.command_id,
                ExecutionCommandState.COMPLETED,
                reason="PROTECTED_TRADE_DURABLY_STORED",
            )
        except AppError as exc:
            target = (
                ExecutionCommandState.BLOCKED
                if exc.code in _BLOCKED_PRE_SUBMISSION_CODES
                else ExecutionCommandState.RECOVERY_REQUIRED
            )
            self._safe_failure_transition(command.command_id, target, exc.code)
            self._last_result = exc.code
            return 0
        except Exception:
            self._safe_failure_transition(
                command.command_id,
                ExecutionCommandState.RECOVERY_REQUIRED,
                "WORKER_UNEXPECTED_FAILURE",
            )
            self._last_result = "WORKER_UNEXPECTED_FAILURE"
            return 0

        self._last_result = "COMPLETED"
        return 1

    def _safe_failure_transition(
        self,
        command_id: str,
        state: ExecutionCommandState,
        reason: str,
    ) -> None:
        try:
            self._commands.transition(
                command_id,
                state,
                reason=reason,
                updates={"failure_reason": reason},
            )
        except Exception:
            self._gate.fail("EXECUTION_COMMAND_PERSISTENCE_FAILED")
            raise

    @staticmethod
    def _validate_trade(command: ExecutionCommand, trade: DemoTradeRecord) -> None:
        if trade.signal_id != command.signal_id:
            raise DemoExecutionWorker._conflict("EXCHANGE_SIGNAL_IDENTITY_CONFLICT")
        if trade.symbol != command.symbol or trade.direction is not command.direction:
            raise DemoExecutionWorker._conflict("EXCHANGE_TRADE_IDENTITY_CONFLICT")
        if trade.client_order_id != command.entry_client_order_id:
            raise DemoExecutionWorker._conflict("ENTRY_CLIENT_ORDER_ID_CONFLICT")
        if trade.stop_client_order_id != command.stop_client_order_id:
            raise DemoExecutionWorker._conflict("STOP_CLIENT_ORDER_ID_CONFLICT")
        if trade.take_profit_client_order_id != command.take_profit_client_order_id:
            raise DemoExecutionWorker._conflict("TAKE_PROFIT_CLIENT_ORDER_ID_CONFLICT")
        if trade.order_status != "FILLED":
            raise DemoExecutionWorker._conflict("ENTRY_FILL_NOT_VERIFIED")
        if trade.executed_quantity <= Decimal("0"):
            raise DemoExecutionWorker._conflict("ENTRY_EXECUTED_QUANTITY_INVALID")
        if trade.executed_quantity > command.approved_quantity:
            raise DemoExecutionWorker._conflict("ENTRY_QUANTITY_EXCEEDS_APPROVAL")
        if not trade.stop_order_id or not trade.take_profit_order_id:
            raise DemoExecutionWorker._conflict("PROTECTION_IDENTITY_MISSING")
        risk_distance = abs(trade.entry_price - trade.stop_loss_price)
        expected_target = (
            trade.entry_price + risk_distance * command.take_profit_r_multiple
            if command.direction.value == "LONG"
            else trade.entry_price - risk_distance * command.take_profit_r_multiple
        )
        if trade.take_profit_price != expected_target:
            raise DemoExecutionWorker._conflict("TAKE_PROFIT_R_MULTIPLE_CONFLICT")

    @staticmethod
    def _conflict(code: str) -> AppError:
        return AppError(
            status_code=409,
            code=code,
            message="Worker exchange result conflicts with the durable execution command",
        )
