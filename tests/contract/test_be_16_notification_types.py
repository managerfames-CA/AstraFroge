"""BE-16 stable notification type contract."""

from app.schemas.notifications import NotificationType


def test_be_16_required_notification_types_are_stable() -> None:
    required = {
        "ORDER_SUBMITTED",
        "ORDER_FINAL_STATUS",
        "PARTIAL_FILL",
        "FULL_FILL",
        "TAKE_PROFIT",
        "STOP_LOSS",
        "RISK_BLOCK",
        "CONNECTION_FAILURE",
        "CONNECTION_RECOVERY",
        "RECONCILIATION_MISMATCH",
        "RECONCILIATION_RESTORED",
    }
    assert required == {item.value for item in NotificationType}
