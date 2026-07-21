"""Durable, typed notification authority."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.persistence.database import Persistence
from app.persistence.notification_models import NotificationRow
from app.schemas.notifications import (
    NotificationReadState,
    NotificationResponse,
    NotificationSeverity,
    NotificationType,
)


class NotificationService:
    """Database-backed, idempotent notification feed."""

    def __init__(self, persistence: Persistence) -> None:
        self._persistence = persistence

    def create_notification(
        self,
        *,
        notification_type: NotificationType,
        severity: NotificationSeverity,
        source_type: str,
        message: str,
        source_identity: str | None = None,
        symbol: str | None = None,
        signal_id: str | None = None,
        trade_id: str | None = None,
        order_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        deduplication_key: str | None = None,
        occurred_at: datetime | None = None,
        outage_cycle_id: str | None = None,
        session: Session | None = None,
    ) -> NotificationResponse:
        """Persist one secret-safe notification and return the canonical row."""

        now = datetime.now(UTC)
        source = source_identity or "DEFAULT"
        cycle_types = {
            NotificationType.CONNECTION_FAILURE,
            NotificationType.CONNECTION_RECOVERY,
            NotificationType.RECONCILIATION_MISMATCH,
            NotificationType.RECONCILIATION_RESTORED,
        }
        if notification_type in cycle_types and outage_cycle_id is None:
            outage_cycle_id = self._current_cycle_id(notification_type, source, session=session)
        if deduplication_key is None:
            identity = outage_cycle_id or source_identity or uuid.uuid4().hex
            deduplication_key = f"{notification_type.value}:{source}:{identity}"

        row = NotificationRow(
            notification_id=f"notif-{uuid.uuid4().hex}",
            deduplication_key=deduplication_key,
            notification_type=notification_type.value,
            severity=severity.value,
            source_type=source_type,
            source_identity=source_identity,
            symbol=symbol,
            signal_id=signal_id,
            trade_id=trade_id,
            order_id=order_id,
            message=self._redact_string(message),
            metadata_json=json.dumps(self._redact_secrets(metadata or {}), sort_keys=True),
            occurred_at=occurred_at or now,
            created_at=now,
            delivery_state=NotificationReadState.UNREAD.value,
        )

        def save(db: Session) -> NotificationResponse:
            existing = db.scalar(
                select(NotificationRow).where(
                    NotificationRow.deduplication_key == deduplication_key
                )
            )
            if existing is not None:
                return self._to_response(existing)
            db.add(row)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                existing = db.scalar(
                    select(NotificationRow).where(
                        NotificationRow.deduplication_key == deduplication_key
                    )
                )
                if existing is None:
                    raise
                return self._to_response(existing)
            return self._to_response(row)

        if session is not None:
            return save(session)
        with self._persistence.transaction() as owned:
            return save(owned)

    def list_notifications(
        self,
        *,
        notification_type: NotificationType | None = None,
        severity: NotificationSeverity | None = None,
        symbol: str | None = None,
        unread_only: bool = False,
    ) -> list[NotificationResponse]:
        with self._persistence.transaction() as session:
            stmt = select(NotificationRow).order_by(
                desc(NotificationRow.occurred_at), desc(NotificationRow.notification_id)
            )
            if notification_type is not None:
                stmt = stmt.where(NotificationRow.notification_type == notification_type.value)
            if severity is not None:
                stmt = stmt.where(NotificationRow.severity == severity.value)
            if symbol is not None:
                stmt = stmt.where(NotificationRow.symbol == symbol)
            if unread_only:
                stmt = stmt.where(
                    NotificationRow.delivery_state == NotificationReadState.UNREAD.value
                )
            return [self._to_response(row) for row in session.scalars(stmt).all()]

    def get_notification(self, notification_id: str) -> NotificationResponse | None:
        with self._persistence.transaction() as session:
            row = session.get(NotificationRow, notification_id)
            return self._to_response(row) if row is not None else None

    def mark_as_read(self, notification_id: str) -> NotificationResponse | None:
        with self._persistence.transaction() as session:
            row = session.get(NotificationRow, notification_id)
            if row is None:
                return None
            row.delivery_state = NotificationReadState.READ.value
            session.flush()
            return self._to_response(row)

    def _current_cycle_id(
        self,
        notification_type: NotificationType,
        source_identity: str,
        *,
        session: Session | None,
    ) -> str:
        failure_types = {
            NotificationType.CONNECTION_FAILURE: NotificationType.CONNECTION_RECOVERY,
            NotificationType.RECONCILIATION_MISMATCH: NotificationType.RECONCILIATION_RESTORED,
        }
        recovery_types = {value: key for key, value in failure_types.items()}

        def query(db: Session) -> str:
            related = (
                [notification_type, failure_types[notification_type]]
                if notification_type in failure_types
                else [recovery_types[notification_type], notification_type]
            )
            row = db.scalar(
                select(NotificationRow)
                .where(NotificationRow.notification_type.in_([item.value for item in related]))
                .where(NotificationRow.source_identity == source_identity)
                .order_by(desc(NotificationRow.occurred_at), desc(NotificationRow.notification_id))
                .limit(1)
            )
            if row is None:
                return "cycle-1"
            if row.notification_type in {
                NotificationType.CONNECTION_RECOVERY.value,
                NotificationType.RECONCILIATION_RESTORED.value,
            }:
                return f"after-{row.notification_id}"
            return str(row.deduplication_key.rsplit(":", 1)[-1])

        if session is not None:
            return query(session)
        with self._persistence.transaction() as owned:
            return query(owned)

    @staticmethod
    def _to_response(row: NotificationRow) -> NotificationResponse:
        metadata = json.loads(row.metadata_json)
        if not isinstance(metadata, dict):
            metadata = {}
        return NotificationResponse(
            notification_id=row.notification_id,
            deduplication_key=row.deduplication_key,
            notification_type=NotificationType(row.notification_type),
            severity=NotificationSeverity(row.severity),
            source_type=row.source_type,
            source_identity=row.source_identity,
            symbol=row.symbol,
            signal_id=row.signal_id,
            trade_id=row.trade_id,
            order_id=row.order_id,
            message=row.message,
            metadata=metadata,
            occurred_at=row.occurred_at,
            created_at=row.created_at,
            delivery_state=NotificationReadState(row.delivery_state),
        )

    @staticmethod
    def _redact_string(value: str) -> str:
        cleaned = re.sub(
            r"(?i)(signature|api[_-]?key|api[_-]?secret|secret|token|password)=([^&\s]+)",
            lambda match: f"{match.group(1)}=[REDACTED]",
            value,
        )
        return re.sub(r"(?i)Bearer\s+[^\s]+", "Bearer [REDACTED]", cleaned)

    @classmethod
    def _redact_secrets(cls, value: Any) -> Any:
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for key, item in value.items():
                if re.search(r"(?i)(secret|token|password|signature|api[_-]?key)", key):
                    cleaned[key] = "[REDACTED]"
                else:
                    cleaned[key] = cls._redact_secrets(item)
            return cleaned
        if isinstance(value, list):
            return [cls._redact_secrets(item) for item in value]
        if isinstance(value, str):
            return cls._redact_string(value)
        return value
