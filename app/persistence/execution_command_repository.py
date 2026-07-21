"""Atomic durable repository for Phase 5 execution commands."""

from __future__ import annotations

import builtins
import hashlib
import json
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, cast

from sqlalchemy import Table, and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.persistence.database import Persistence
from app.persistence.models import ExecutionCommandRow, ExecutionCommandTransitionRow
from app.schemas.execution_command import (
    ExecutionCommand,
    ExecutionCommandState,
    ExecutionCommandTransition,
)

_ALLOWED_TRANSITIONS: dict[ExecutionCommandState, frozenset[ExecutionCommandState]] = {
    ExecutionCommandState.PENDING: frozenset(
        {
            ExecutionCommandState.CLAIMED,
            ExecutionCommandState.BLOCKED,
            ExecutionCommandState.EXPIRED,
        }
    ),
    ExecutionCommandState.CLAIMED: frozenset(
        {
            ExecutionCommandState.SUBMITTING,
            ExecutionCommandState.BLOCKED,
            ExecutionCommandState.FAILED,
            ExecutionCommandState.RECOVERY_REQUIRED,
            ExecutionCommandState.EXPIRED,
        }
    ),
    ExecutionCommandState.SUBMITTING: frozenset(
        {
            ExecutionCommandState.ENTRY_CONFIRMED,
            ExecutionCommandState.BLOCKED,
            ExecutionCommandState.FAILED,
            ExecutionCommandState.RECOVERY_REQUIRED,
        }
    ),
    ExecutionCommandState.ENTRY_CONFIRMED: frozenset(
        {
            ExecutionCommandState.PROTECTION_PENDING,
            ExecutionCommandState.RECOVERY_REQUIRED,
        }
    ),
    ExecutionCommandState.PROTECTION_PENDING: frozenset(
        {
            ExecutionCommandState.PROTECTED,
            ExecutionCommandState.FAILED,
            ExecutionCommandState.RECOVERY_REQUIRED,
        }
    ),
    ExecutionCommandState.PROTECTED: frozenset(
        {
            ExecutionCommandState.COMPLETED,
            ExecutionCommandState.RECOVERY_REQUIRED,
        }
    ),
    ExecutionCommandState.RECOVERY_REQUIRED: frozenset(
        {
            ExecutionCommandState.CLAIMED,
            ExecutionCommandState.BLOCKED,
            ExecutionCommandState.EXPIRED,
        }
    ),
    ExecutionCommandState.COMPLETED: frozenset(),
    ExecutionCommandState.BLOCKED: frozenset(),
    ExecutionCommandState.FAILED: frozenset(),
    ExecutionCommandState.EXPIRED: frozenset(),
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Execution-command timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


class IllegalExecutionCommandTransition(ValueError):
    """Raised when a caller attempts a non-deterministic state transition."""


class ExecutionCommandRepository:
    """PostgreSQL/SQLite durable queue with atomic idempotency and claiming."""

    def __init__(self, persistence: Persistence) -> None:
        self.persistence = persistence
        self._non_postgresql_claim_lock = Lock()

    @property
    def available(self) -> bool:
        return True

    def create(self, command: ExecutionCommand) -> ExecutionCommand:
        """Insert once by deterministic identity and return the durable command."""

        with self.persistence.transaction() as session:
            existing = self._by_identity(session, command.idempotency_key)
            if existing is not None:
                return self._model(existing)

            values = {
                "command_id": command.command_id,
                "idempotency_key": command.idempotency_key,
                "signal_id": command.signal_id,
                "decision_key": command.decision_key,
                "risk_decision_id": command.risk_decision_id,
                "source_snapshot_version": command.source_snapshot_version,
                "symbol": command.symbol,
                "state": command.state.value,
                "payload_json": self._payload(command),
                "claim_token": command.claim_token,
                "worker_id": command.worker_id,
                "claimed_at": command.claimed_at,
                "failure_reason": command.failure_reason,
                "entry_exchange_order_id": command.entry_exchange_order_id,
                "stop_exchange_order_id": command.stop_exchange_order_id,
                "take_profit_exchange_order_id": command.take_profit_exchange_order_id,
                "created_at": _utc(command.created_at),
                "updated_at": _utc(command.updated_at),
                "expires_at": _utc(command.expires_at),
            }
            inserted = self._insert_command(session, values)
            if inserted:
                self._append_transition(
                    session,
                    command_id=command.command_id,
                    sequence=1,
                    from_state=None,
                    to_state=ExecutionCommandState.PENDING,
                    reason="COMMAND_CREATED",
                    changed_at=command.created_at,
                )
                return command

            concurrent = self._by_identity(session, command.idempotency_key)
            if concurrent is None:
                raise RuntimeError(
                    "Execution command insert conflicted without a durable row"
                )
            return self._model(concurrent)

    def get(self, command_id: str) -> ExecutionCommand | None:
        with self.persistence.transaction() as session:
            row = session.get(ExecutionCommandRow, command_id)
            return self._model(row) if row is not None else None

    def list(self, *, limit: int = 500) -> builtins.list[ExecutionCommand]:
        with self.persistence.transaction() as session:
            statement = (
                select(ExecutionCommandRow)
                .order_by(ExecutionCommandRow.created_at.desc())
                .limit(limit)
            )
            return [self._model(row) for row in session.scalars(statement)]

    def history(
        self,
        command_id: str,
    ) -> builtins.list[ExecutionCommandTransition]:
        with self.persistence.transaction() as session:
            statement = (
                select(ExecutionCommandTransitionRow)
                .where(ExecutionCommandTransitionRow.command_id == command_id)
                .order_by(ExecutionCommandTransitionRow.sequence)
            )
            return [
                ExecutionCommandTransition(
                    sequence=row.sequence,
                    from_state=(
                        ExecutionCommandState(row.from_state)
                        if row.from_state is not None
                        else None
                    ),
                    to_state=ExecutionCommandState(row.to_state),
                    reason=row.reason,
                    changed_at=_stored_utc(row.changed_at),
                )
                for row in session.scalars(statement)
            ]

    def transition(
        self,
        command_id: str,
        to_state: ExecutionCommandState,
        *,
        reason: str,
        changed_at: datetime,
        updates: dict[str, Any] | None = None,
    ) -> ExecutionCommand:
        with self.persistence.transaction() as session:
            row = self._locked_row(session, command_id)
            if row is None:
                raise KeyError(command_id)
            return self._apply_transition(
                session,
                row,
                to_state,
                reason=reason,
                changed_at=changed_at,
                updates=updates,
            )

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        claim_timeout_seconds: float = 30.0,
    ) -> ExecutionCommand | None:
        """Atomically claim one pending/recoverable command; concurrent workers skip it."""

        if self.persistence.engine.dialect.name == "postgresql":
            return self._claim_next(
                worker_id=worker_id,
                now=now,
                claim_timeout_seconds=claim_timeout_seconds,
            )
        with self._non_postgresql_claim_lock:
            return self._claim_next(
                worker_id=worker_id,
                now=now,
                claim_timeout_seconds=claim_timeout_seconds,
            )

    def _claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        claim_timeout_seconds: float,
    ) -> ExecutionCommand | None:
        current = _utc(now)
        cutoff = current - timedelta(seconds=claim_timeout_seconds)
        with self.persistence.transaction() as session:
            self._expire_due(session, current)
            stale_claim = and_(
                ExecutionCommandRow.state.in_(
                    [
                        ExecutionCommandState.CLAIMED.value,
                        ExecutionCommandState.SUBMITTING.value,
                    ]
                ),
                ExecutionCommandRow.claimed_at.is_not(None),
                ExecutionCommandRow.claimed_at <= cutoff,
            )
            statement = (
                select(ExecutionCommandRow)
                .where(
                    or_(
                        ExecutionCommandRow.state.in_(
                            [
                                ExecutionCommandState.PENDING.value,
                                ExecutionCommandState.RECOVERY_REQUIRED.value,
                            ]
                        ),
                        stale_claim,
                    ),
                    ExecutionCommandRow.expires_at > current,
                )
                .order_by(ExecutionCommandRow.created_at)
                .limit(1)
            )
            if session.get_bind().dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            else:
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                return None

            prior = ExecutionCommandState(row.state)
            if prior in {
                ExecutionCommandState.CLAIMED,
                ExecutionCommandState.SUBMITTING,
            }:
                row.state = ExecutionCommandState.RECOVERY_REQUIRED.value
                recovered = self._model(row).model_copy(
                    update={
                        "state": ExecutionCommandState.RECOVERY_REQUIRED,
                        "updated_at": current,
                        "failure_reason": "STALE_WORKER_CLAIM",
                    }
                )
                row.payload_json = self._payload(recovered)
                row.failure_reason = "STALE_WORKER_CLAIM"
                row.updated_at = current
                self._append_transition(
                    session,
                    command_id=row.command_id,
                    sequence=self._next_sequence(session, row.command_id),
                    from_state=prior,
                    to_state=ExecutionCommandState.RECOVERY_REQUIRED,
                    reason="STALE_WORKER_CLAIM",
                    changed_at=current,
                )

            claim_token = hashlib.sha256(
                f"{row.command_id}|{worker_id}|{current.isoformat()}".encode()
            ).hexdigest()
            return self._apply_transition(
                session,
                row,
                ExecutionCommandState.CLAIMED,
                reason="WORKER_CLAIMED",
                changed_at=current,
                updates={
                    "claim_token": claim_token,
                    "worker_id": worker_id,
                    "claimed_at": current,
                    "failure_reason": None,
                },
            )

    def counts(self) -> tuple[dict[ExecutionCommandState, int], datetime | None]:
        with self.persistence.transaction() as session:
            rows = session.execute(
                select(ExecutionCommandRow.state, func.count()).group_by(
                    ExecutionCommandRow.state
                )
            ).all()
            updated = session.scalar(select(func.max(ExecutionCommandRow.updated_at)))
        counts = {state: 0 for state in ExecutionCommandState}
        for state, count in rows:
            counts[ExecutionCommandState(str(state))] = int(count)
        return counts, _stored_utc(updated) if updated is not None else None

    def _expire_due(self, session: Session, now: datetime) -> None:
        statement = select(ExecutionCommandRow).where(
            ExecutionCommandRow.state.in_(
                [
                    ExecutionCommandState.PENDING.value,
                    ExecutionCommandState.CLAIMED.value,
                    ExecutionCommandState.RECOVERY_REQUIRED.value,
                ]
            ),
            ExecutionCommandRow.expires_at <= now,
        )
        if session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        for row in session.scalars(statement):
            self._apply_transition(
                session,
                row,
                ExecutionCommandState.EXPIRED,
                reason="COMMAND_EXPIRED",
                changed_at=now,
            )

    def _apply_transition(
        self,
        session: Session,
        row: ExecutionCommandRow,
        to_state: ExecutionCommandState,
        *,
        reason: str,
        changed_at: datetime,
        updates: dict[str, Any] | None = None,
    ) -> ExecutionCommand:
        from_state = ExecutionCommandState(row.state)
        if to_state is from_state:
            return self._model(row)
        if to_state not in _ALLOWED_TRANSITIONS[from_state]:
            raise IllegalExecutionCommandTransition(
                f"Illegal execution-command transition {from_state.value}->{to_state.value}"
            )

        current = _utc(changed_at)
        command = self._model(row)
        audit_codes = list(command.audit_codes)
        if reason not in audit_codes:
            audit_codes.append(reason)
        model_updates: dict[str, Any] = {
            "state": to_state,
            "updated_at": current,
            "audit_codes": audit_codes,
        }
        if updates:
            model_updates.update(updates)
        command = command.model_copy(update=model_updates)

        row.state = to_state.value
        row.payload_json = self._payload(command)
        row.updated_at = current
        row.claim_token = command.claim_token
        row.worker_id = command.worker_id
        row.claimed_at = command.claimed_at
        row.failure_reason = command.failure_reason
        row.entry_exchange_order_id = command.entry_exchange_order_id
        row.stop_exchange_order_id = command.stop_exchange_order_id
        row.take_profit_exchange_order_id = command.take_profit_exchange_order_id
        self._append_transition(
            session,
            command_id=row.command_id,
            sequence=self._next_sequence(session, row.command_id),
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            changed_at=current,
        )
        return command

    @staticmethod
    def _next_sequence(session: Session, command_id: str) -> int:
        maximum = session.scalar(
            select(func.max(ExecutionCommandTransitionRow.sequence)).where(
                ExecutionCommandTransitionRow.command_id == command_id
            )
        )
        return int(maximum or 0) + 1

    @staticmethod
    def _append_transition(
        session: Session,
        *,
        command_id: str,
        sequence: int,
        from_state: ExecutionCommandState | None,
        to_state: ExecutionCommandState,
        reason: str,
        changed_at: datetime,
    ) -> None:
        event_id = hashlib.sha256(f"{command_id}:{sequence}".encode()).hexdigest()
        session.add(
            ExecutionCommandTransitionRow(
                event_id=event_id,
                command_id=command_id,
                sequence=sequence,
                from_state=from_state.value if from_state is not None else None,
                to_state=to_state.value,
                reason=reason,
                payload_json=_json(
                    {
                        "from_state": (
                            from_state.value if from_state is not None else None
                        ),
                        "to_state": to_state.value,
                        "reason": reason,
                    }
                ),
                changed_at=_utc(changed_at),
            )
        )

    @staticmethod
    def _locked_row(
        session: Session,
        command_id: str,
    ) -> ExecutionCommandRow | None:
        statement = select(ExecutionCommandRow).where(
            ExecutionCommandRow.command_id == command_id
        )
        return session.scalar(statement.with_for_update())

    @staticmethod
    def _by_identity(
        session: Session,
        idempotency_key: str,
    ) -> ExecutionCommandRow | None:
        return session.scalar(
            select(ExecutionCommandRow).where(
                ExecutionCommandRow.idempotency_key == idempotency_key
            )
        )

    @staticmethod
    def _model(row: ExecutionCommandRow) -> ExecutionCommand:
        return ExecutionCommand.model_validate_json(row.payload_json)

    @staticmethod
    def _payload(command: ExecutionCommand) -> str:
        return command.model_dump_json()

    @staticmethod
    def _insert_command(session: Session, values: dict[str, Any]) -> bool:
        table = cast(Table, ExecutionCommandRow.__table__)
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            result = cast(
                CursorResult[Any],
                session.execute(
                    postgresql_insert(table).values(**values).on_conflict_do_nothing()
                ),
            )
            return result.rowcount > 0
        if dialect == "sqlite":
            result = cast(
                CursorResult[Any],
                session.execute(
                    sqlite_insert(table).values(**values).on_conflict_do_nothing()
                ),
            )
            return result.rowcount > 0
        session.add(ExecutionCommandRow(**values))
        session.flush()
        return True
