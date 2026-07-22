"""Phase 5 READY-to-Risk-to-durable-command boundary."""

from __future__ import annotations

import builtins
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.core.config import Settings
from app.core.errors import AppError
from app.persistence.execution_command_repository import ExecutionCommandRepository
from app.schemas.execution_command import (
    ExecutionCommand,
    ExecutionCommandList,
    ExecutionCommandState,
    ExecutionCommandStatus,
    ExecutionCommandTransition,
)
from app.schemas.risk import RiskAssessment, RiskDecision
from app.schemas.scanner import ScannerDirection, ScannerGrade
from app.schemas.signal_decision import EntryTriggerStatus, SignalDecisionStatus
from app.schemas.signals import SignalLifecycle, SignalRecord
from app.services.account_snapshot import AccountSnapshot, AccountSnapshotService
from app.services.risk import RiskService
from app.services.signals import SignalService

_D0 = Decimal("0")
_OPERATION = "OPEN_PROTECTED_DEMO_TRADE"


class ExecutionCommandService:
    """Create immutable commands only from current READY and Risk-approved facts."""

    def __init__(
        self,
        signal_service: SignalService,
        risk_service: RiskService,
        settings: Settings,
        account_snapshots: AccountSnapshotService | None,
        repository: ExecutionCommandRepository | None,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._signals = signal_service
        self._risk = risk_service
        self._settings = settings
        self._snapshots = account_snapshots
        self._repository = repository
        self._now = now_provider or (lambda: datetime.now(UTC))

    @property
    def persistence_available(self) -> bool:
        return self._repository is not None

    def enqueue(self, signal_id: str) -> ExecutionCommand:
        """Persist one command before any Binance write path can observe it."""

        repository = self._required_repository()
        signal, assessment, snapshot = self._current_approval(signal_id)
        created_at = self._now().astimezone(UTC)
        expires_at = signal.qualification_expires_at or signal.expires_at
        if expires_at <= created_at:
            raise self._blocked("EXECUTION_DECISION_EXPIRED")

        quantity = self._required_decimal(
            assessment.recommended_quantity,
            "RISK_QUANTITY_UNAVAILABLE",
        )
        notional = self._required_decimal(
            assessment.position_notional_usdt,
            "RISK_NOTIONAL_UNAVAILABLE",
        )
        margin = self._required_decimal(
            assessment.required_margin_usdt,
            "RISK_MARGIN_UNAVAILABLE",
        )
        stop = self._required_decimal(
            assessment.stop_loss_price,
            "RISK_STOP_UNAVAILABLE",
        )
        grade = self._required_grade(signal.grade)
        leverage = self._leverage(snapshot, signal.symbol)
        self._validate_plan_consistency(
            signal=signal,
            assessment=assessment,
            quantity=quantity,
            notional=notional,
            margin=margin,
            leverage=leverage,
            stop=stop,
        )
        take_profit = self._take_profit(
            direction=signal.direction,
            entry=assessment.entry_trigger_price,
            stop=stop,
        )
        risk_decision_id = self._risk_decision_id(assessment, snapshot.snapshot_id)
        identity_payload = {
            "operation": _OPERATION,
            "signal_id": signal.signal_id,
            "decision_key": signal.decision_key,
            "source_snapshot_version": signal.source_snapshot_version,
        }
        deterministic_identity = self._digest(identity_payload)
        entry_id, stop_id, take_id = self.client_order_ids(signal.signal_id)
        command = ExecutionCommand(
            command_id=deterministic_identity,
            idempotency_key=deterministic_identity,
            deterministic_execution_identity=deterministic_identity,
            signal_id=signal.signal_id,
            decision_key=self._required_text(signal.decision_key, "MISSING_DECISION_KEY"),
            risk_decision_id=risk_decision_id,
            symbol=signal.symbol,
            direction=signal.direction,
            setup=signal.setup,
            grade=grade,
            source_snapshot_version=self._required_text(
                signal.source_snapshot_version,
                "MISSING_SOURCE_PROVENANCE",
            ),
            entry_trigger_price=assessment.entry_trigger_price,
            stop_loss_price=stop,
            take_profit_price=take_profit,
            take_profit_r_multiple=self._settings.execution_take_profit_r_multiple,
            approved_quantity=quantity,
            approved_notional=notional,
            approved_margin=margin,
            leverage=leverage,
            account_snapshot_id=snapshot.snapshot_id,
            state=ExecutionCommandState.PENDING,
            created_at=created_at,
            updated_at=created_at,
            expires_at=expires_at,
            entry_client_order_id=entry_id,
            stop_client_order_id=stop_id,
            take_profit_client_order_id=take_id,
            audit_codes=["READY_RISK_APPROVED", "COMMAND_PERSISTED_BEFORE_SUBMISSION"],
        )
        return repository.create(command)

    def enqueue_all_ready(self) -> int:
        """Queue current approvals; never call Binance and never bypass command idempotency."""

        created = 0
        before = {item.command_id for item in self.list().commands}
        for assessment in self._risk.assessments().assessments:
            if assessment.decision is not RiskDecision.APPROVED:
                continue
            try:
                command = self.enqueue(assessment.signal_id)
            except AppError:
                continue
            if command.command_id not in before:
                before.add(command.command_id)
                created += 1
        return created

    def revalidate(
        self,
        command: ExecutionCommand,
    ) -> tuple[SignalRecord, RiskAssessment, AccountSnapshot]:
        """Re-prove READY, Risk, account and command consistency immediately before entry."""

        signal, assessment, snapshot = self._current_approval(command.signal_id)
        now = self._now().astimezone(UTC)
        if command.expires_at <= now:
            raise self._blocked("EXECUTION_COMMAND_EXPIRED")
        if signal.decision_key != command.decision_key:
            raise self._blocked("DECISION_KEY_SUPERSEDED")
        if signal.source_snapshot_version != command.source_snapshot_version:
            raise self._blocked("SOURCE_SNAPSHOT_SUPERSEDED")
        if (
            signal.direction is not command.direction
            or assessment.direction is not command.direction
        ):
            raise self._blocked("EXECUTION_DIRECTION_CONFLICT")
        if assessment.entry_trigger_price != command.entry_trigger_price:
            raise self._blocked("ENTRY_TRIGGER_CONFLICT")
        if assessment.stop_loss_price != command.stop_loss_price:
            raise self._blocked("STOP_LOSS_CONFLICT")
        if assessment.recommended_quantity != command.approved_quantity:
            raise self._blocked("APPROVED_QUANTITY_CONFLICT")
        if assessment.position_notional_usdt != command.approved_notional:
            raise self._blocked("APPROVED_NOTIONAL_CONFLICT")
        if assessment.required_margin_usdt != command.approved_margin:
            raise self._blocked("APPROVED_MARGIN_CONFLICT")
        if self._leverage(snapshot, command.symbol) != command.leverage:
            raise self._blocked("LEVERAGE_CONFLICT")
        expected_target = self._take_profit(
            direction=command.direction,
            entry=command.entry_trigger_price,
            stop=command.stop_loss_price,
        )
        if expected_target != command.take_profit_price:
            raise self._blocked("TAKE_PROFIT_CONFLICT")
        for position in snapshot.positions:
            if position.symbol == command.symbol and position.position_amount != _D0:
                raise self._blocked("EXISTING_EXCHANGE_POSITION_CONFLICT")
        return signal, assessment, snapshot

    def get(self, command_id: str) -> ExecutionCommand | None:
        repository = self._required_repository()
        return repository.get(command_id)

    def list(self) -> ExecutionCommandList:
        repository = self._required_repository()
        commands = repository.list()
        return ExecutionCommandList(count=len(commands), commands=commands)

    def history(
        self,
        command_id: str,
    ) -> builtins.list[ExecutionCommandTransition]:
        return self._required_repository().history(command_id)

    def status(self) -> ExecutionCommandStatus:
        if self._repository is None:
            return ExecutionCommandStatus(
                execution_enabled=self._settings.execution_enabled,
                persistence_available=False,
                pending=0,
                claimed=0,
                recovery_required=0,
                completed=0,
                blocked=0,
                failed=0,
                expired=0,
            )
        counts, updated_at = self._repository.counts()
        active_claimed = sum(
            counts[state]
            for state in (
                ExecutionCommandState.CLAIMED,
                ExecutionCommandState.SUBMITTING,
                ExecutionCommandState.ENTRY_CONFIRMED,
                ExecutionCommandState.PROTECTION_PENDING,
                ExecutionCommandState.PROTECTED,
            )
        )
        return ExecutionCommandStatus(
            execution_enabled=self._settings.execution_enabled,
            persistence_available=True,
            pending=counts[ExecutionCommandState.PENDING],
            claimed=active_claimed,
            recovery_required=counts[ExecutionCommandState.RECOVERY_REQUIRED],
            completed=counts[ExecutionCommandState.COMPLETED],
            blocked=counts[ExecutionCommandState.BLOCKED],
            failed=counts[ExecutionCommandState.FAILED],
            expired=counts[ExecutionCommandState.EXPIRED],
            updated_at=updated_at,
        )

    def claim_next(self, worker_id: str) -> ExecutionCommand | None:
        return self._required_repository().claim_next(
            worker_id=worker_id,
            now=self._now().astimezone(UTC),
        )

    def transition(
        self,
        command_id: str,
        state: ExecutionCommandState,
        *,
        reason: str,
        updates: dict[str, Any] | None = None,
    ) -> ExecutionCommand:
        return self._required_repository().transition(
            command_id,
            state,
            reason=reason,
            changed_at=self._now().astimezone(UTC),
            updates=updates,
        )

    def _current_approval(
        self,
        signal_id: str,
    ) -> tuple[SignalRecord, RiskAssessment, AccountSnapshot]:
        snapshots = self._snapshots
        if snapshots is None:
            raise self._blocked("ACCOUNT_SNAPSHOT_UNAVAILABLE")
        try:
            snapshot = snapshots.force_refresh()
        except Exception as exc:
            raise self._blocked("ACCOUNT_SNAPSHOT_REFRESH_FAILED") from exc
        status = snapshots.status()
        if not status.fresh or status.snapshot_id != snapshot.snapshot_id:
            raise self._blocked("ACCOUNT_SNAPSHOT_STALE")
        if not snapshot.source_healthy or not snapshot.can_trade:
            raise self._blocked("ACCOUNT_NOT_EXECUTABLE")

        signal = self._signals.get(signal_id)
        if signal is None:
            raise AppError(status_code=404, code="SIGNAL_NOT_FOUND", message="Signal not found")
        self._validate_signal(signal)
        assessment = next(
            (item for item in self._risk.assessments().assessments if item.signal_id == signal_id),
            None,
        )
        if assessment is None:
            raise self._blocked("RISK_ASSESSMENT_MISSING")
        if assessment.updated_at < snapshot.captured_at:
            raise self._blocked("RISK_ASSESSMENT_STALE")
        if (
            assessment.decision is not RiskDecision.APPROVED
            or not assessment.approved_for_execution
            or assessment.blocked_reason is not None
        ):
            raise self._blocked("RISK_NOT_APPROVED")
        return signal, assessment, snapshot

    def _validate_signal(self, signal: SignalRecord) -> None:
        now = self._now().astimezone(UTC)
        if signal.decision_status is not SignalDecisionStatus.READY:
            raise self._blocked("DECISION_NOT_READY")
        if signal.lifecycle is not SignalLifecycle.ACTIVE:
            raise self._blocked("SIGNAL_NOT_ACTIVE")
        if signal.grade not in {ScannerGrade.A_PLUS, ScannerGrade.A}:
            raise self._blocked("GRADE_NOT_EXECUTABLE")
        if (
            signal.entry_trigger_status is not EntryTriggerStatus.READY
            or not signal.entry_ready
            or not signal.ready
            or not signal.selected
        ):
            raise self._blocked("ENTRY_TRIGGER_NOT_READY")
        if signal.rejection_reasons or signal.watch_reasons:
            raise self._blocked("DECISION_HAS_BLOCKING_OR_WATCH_REASONS")
        if not signal.decision_fresh:
            raise self._blocked("DECISION_STALE")
        if not signal.decision_key:
            raise self._blocked("MISSING_DECISION_KEY")
        if not signal.source_snapshot_version:
            raise self._blocked("MISSING_SOURCE_PROVENANCE")
        expiry = signal.qualification_expires_at or signal.expires_at
        if expiry <= now:
            raise self._blocked("EXECUTION_DECISION_EXPIRED")

    def _validate_plan_consistency(
        self,
        *,
        signal: SignalRecord,
        assessment: RiskAssessment,
        quantity: Decimal,
        notional: Decimal,
        margin: Decimal,
        leverage: int,
        stop: Decimal,
    ) -> None:
        if assessment.direction is not signal.direction:
            raise self._blocked("EXECUTION_DIRECTION_CONFLICT")
        if quantity * assessment.entry_trigger_price != notional:
            raise self._blocked("APPROVED_NOTIONAL_CONFLICT")
        if notional / Decimal(leverage) != margin:
            raise self._blocked("APPROVED_MARGIN_CONFLICT")
        if signal.direction is ScannerDirection.LONG and stop >= assessment.entry_trigger_price:
            raise self._blocked("STOP_LOSS_CONFLICT")
        if signal.direction is ScannerDirection.SHORT and stop <= assessment.entry_trigger_price:
            raise self._blocked("STOP_LOSS_CONFLICT")

    def _take_profit(
        self,
        *,
        direction: ScannerDirection,
        entry: Decimal,
        stop: Decimal,
    ) -> Decimal:
        multiple = self._settings.execution_take_profit_r_multiple
        if multiple <= _D0:
            raise self._blocked("TAKE_PROFIT_POLICY_NOT_CONFIGURED")
        distance = abs(entry - stop)
        if distance <= _D0:
            raise self._blocked("STOP_DISTANCE_INVALID")
        if direction is ScannerDirection.LONG:
            return entry + distance * multiple
        return entry - distance * multiple

    @staticmethod
    def _leverage(snapshot: AccountSnapshot, symbol: str) -> int:
        for position in snapshot.positions:
            if position.symbol == symbol:
                return position.leverage
        raise ExecutionCommandService._blocked("SYMBOL_LEVERAGE_UNAVAILABLE")

    @staticmethod
    def _risk_decision_id(assessment: RiskAssessment, snapshot_id: str) -> str:
        payload = assessment.model_dump(mode="json", exclude={"updated_at"})
        payload["account_snapshot_id"] = snapshot_id
        return ExecutionCommandService._digest(payload)

    @staticmethod
    def client_order_ids(signal_id: str) -> tuple[str, str, str]:
        prefix = signal_id[:20]
        return f"af-e-{prefix}", f"af-s-{prefix}", f"af-t-{prefix}"

    @staticmethod
    def _digest(payload: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()

    @staticmethod
    def _required_text(value: str | None, code: str) -> str:
        if value:
            return value
        raise ExecutionCommandService._blocked(code)

    @staticmethod
    def _required_decimal(value: Decimal | None, code: str) -> Decimal:
        if value is not None and value.is_finite() and value > _D0:
            return value
        raise ExecutionCommandService._blocked(code)

    @staticmethod
    def _required_grade(value: ScannerGrade | None) -> ScannerGrade:
        if value in {ScannerGrade.A_PLUS, ScannerGrade.A}:
            return value
        raise ExecutionCommandService._blocked("GRADE_NOT_EXECUTABLE")

    @staticmethod
    def _blocked(code: str) -> AppError:
        return AppError(
            status_code=409,
            code=code,
            message="Execution command eligibility could not be proven",
        )

    def _required_repository(self) -> ExecutionCommandRepository:
        if self._repository is None:
            raise AppError(
                status_code=503,
                code="EXECUTION_COMMAND_PERSISTENCE_REQUIRED",
                message="Durable persistence is required for execution commands",
            )
        return self._repository
