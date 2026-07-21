"""Typed startup recovery and automation-readiness contracts."""

from __future__ import annotations

from enum import StrEnum


class RecoveryState(StrEnum):
    """Authoritative startup recovery lifecycle for automated Demo execution."""

    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RECOVERING = "RECOVERING"
    EXCHANGE_RECONCILED = "EXCHANGE_RECONCILED"
    SIGNALS_REVALIDATED = "SIGNALS_REVALIDATED"
    AUTOMATION_READY = "AUTOMATION_READY"
    RECOVERY_FAILED = "RECOVERY_FAILED"
