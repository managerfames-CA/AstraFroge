"""Typed durable notification contracts for BE-16."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class NotificationType(StrEnum):
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_FINAL_STATUS = "ORDER_FINAL_STATUS"
    PARTIAL_FILL = "PARTIAL_FILL"
    FULL_FILL = "FULL_FILL"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    RISK_BLOCK = "RISK_BLOCK"
    CONNECTION_FAILURE = "CONNECTION_FAILURE"
    CONNECTION_RECOVERY = "CONNECTION_RECOVERY"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    RECONCILIATION_RESTORED = "RECONCILIATION_RESTORED"


class NotificationSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class NotificationReadState(StrEnum):
    UNREAD = "UNREAD"
    READ = "READ"


class NotificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notification_id: str
    deduplication_key: str
    notification_type: NotificationType
    severity: NotificationSeverity
    source_type: str
    source_identity: str | None = None
    symbol: str | None = None
    signal_id: str | None = None
    trade_id: str | None = None
    order_id: str | None = None
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime
    created_at: datetime
    delivery_state: NotificationReadState

    @field_validator("occurred_at", "created_at")
    @classmethod
    def normalize_utc_datetime(cls, value: datetime) -> datetime:
        """Keep notification timestamps consistently timezone-aware in UTC."""

        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class NotificationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=0)
    notifications: list[NotificationResponse] = Field(default_factory=list)


class NotificationStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unread_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    is_active: bool
