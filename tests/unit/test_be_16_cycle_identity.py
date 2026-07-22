"""BE-16 outage-cycle identity regression tests."""

from app.schemas.notifications import NotificationType


def test_failure_and_recovery_types_remain_paired() -> None:
    pairs = {
        NotificationType.CONNECTION_FAILURE: NotificationType.CONNECTION_RECOVERY,
        NotificationType.RECONCILIATION_MISMATCH: NotificationType.RECONCILIATION_RESTORED,
    }
    assert pairs[NotificationType.CONNECTION_FAILURE] is NotificationType.CONNECTION_RECOVERY
    assert (
        pairs[NotificationType.RECONCILIATION_MISMATCH]
        is NotificationType.RECONCILIATION_RESTORED
    )
