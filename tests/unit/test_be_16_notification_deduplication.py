"""Focused BE-16 notification authority tests."""

from app.schemas.notifications import NotificationType


def test_cycle_types_are_explicit() -> None:
    assert NotificationType.CONNECTION_FAILURE.value == "CONNECTION_FAILURE"
    assert NotificationType.CONNECTION_RECOVERY.value == "CONNECTION_RECOVERY"
    assert NotificationType.RECONCILIATION_MISMATCH.value == "RECONCILIATION_MISMATCH"
    assert NotificationType.RECONCILIATION_RESTORED.value == "RECONCILIATION_RESTORED"


def test_fill_and_protective_types_are_explicit() -> None:
    assert NotificationType.PARTIAL_FILL.value == "PARTIAL_FILL"
    assert NotificationType.FULL_FILL.value == "FULL_FILL"
    assert NotificationType.STOP_LOSS.value == "STOP_LOSS"
    assert NotificationType.TAKE_PROFIT.value == "TAKE_PROFIT"
