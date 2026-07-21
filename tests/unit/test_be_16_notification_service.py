"""Focused BE-16 notification service coverage."""

from pathlib import Path

from app.persistence.database import Persistence
from app.persistence.models import Base
from app.persistence.notification_models import NotificationRow  # noqa: F401
from app.schemas.notifications import (
    NotificationReadState,
    NotificationSeverity,
    NotificationType,
)
from app.services.notifications import NotificationService


def _service(tmp_path: Path) -> NotificationService:
    persistence = Persistence(f"sqlite:///{tmp_path / 'notifications.db'}")
    Base.metadata.create_all(persistence.engine)
    return NotificationService(persistence)


def test_notification_crud_filters_redaction_and_deduplication(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.create_notification(
        notification_type=NotificationType.ORDER_SUBMITTED,
        severity=NotificationSeverity.INFO,
        source_type="ORDER",
        source_identity="order-1",
        symbol="BTCUSDT",
        order_id="123",
        message="api_key=secret Bearer hidden",
        metadata={"token": "secret", "nested": {"password": "hidden"}},
        deduplication_key="order:1",
    )
    duplicate = service.create_notification(
        notification_type=NotificationType.ORDER_SUBMITTED,
        severity=NotificationSeverity.INFO,
        source_type="ORDER",
        message="duplicate",
        deduplication_key="order:1",
    )

    assert duplicate.notification_id == created.notification_id
    assert "secret" not in created.message
    assert created.metadata == {"nested": {"password": "[REDACTED]"}, "token": "[REDACTED]"}
    assert service.get_notification(created.notification_id) == created
    assert service.get_notification("missing") is None
    assert service.list_notifications(symbol="BTCUSDT") == [created]
    assert service.list_notifications(
        notification_type=NotificationType.ORDER_SUBMITTED
    ) == [created]
    assert service.list_notifications(severity=NotificationSeverity.INFO) == [created]
    assert service.list_notifications(unread_only=True) == [created]

    read = service.mark_as_read(created.notification_id)
    assert read is not None
    assert read.delivery_state is NotificationReadState.READ
    assert service.mark_as_read("missing") is None
    assert service.list_notifications(unread_only=True) == []


def test_outage_cycles_create_new_identity_after_recovery(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first_failure = service.create_notification(
        notification_type=NotificationType.CONNECTION_FAILURE,
        severity=NotificationSeverity.CRITICAL,
        source_type="BINANCE",
        source_identity="demo-api",
        message="connection failed",
    )
    repeated_failure = service.create_notification(
        notification_type=NotificationType.CONNECTION_FAILURE,
        severity=NotificationSeverity.CRITICAL,
        source_type="BINANCE",
        source_identity="demo-api",
        message="connection failed again",
    )
    recovery = service.create_notification(
        notification_type=NotificationType.CONNECTION_RECOVERY,
        severity=NotificationSeverity.INFO,
        source_type="BINANCE",
        source_identity="demo-api",
        message="connection restored",
    )
    next_failure = service.create_notification(
        notification_type=NotificationType.CONNECTION_FAILURE,
        severity=NotificationSeverity.CRITICAL,
        source_type="BINANCE",
        source_identity="demo-api",
        message="new outage",
    )

    assert repeated_failure.notification_id == first_failure.notification_id
    assert recovery.notification_id != first_failure.notification_id
    assert next_failure.notification_id != first_failure.notification_id
    assert next_failure.deduplication_key.endswith(f"after-{recovery.notification_id}")
    assert len(service.list_notifications()) == 3
