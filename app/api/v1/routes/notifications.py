"""Durable notification API routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.v1.notification_dependencies import get_notification_service
from app.core.security import MutationAuthorization, authorize_mutation
from app.schemas.notifications import (
    NotificationListResponse,
    NotificationResponse,
    NotificationSeverity,
    NotificationStatusResponse,
    NotificationType,
)
from app.services.notifications import NotificationService

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/status", response_model=NotificationStatusResponse)
async def notification_status(
    service: NotificationService = Depends(get_notification_service),  # noqa: B008
) -> NotificationStatusResponse:
    notifications = service.list_notifications()
    unread = [item for item in notifications if item.delivery_state.value == "UNREAD"]
    return NotificationStatusResponse(
        unread_count=len(unread),
        total_count=len(notifications),
        is_active=True,
    )


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    service: NotificationService = Depends(get_notification_service),  # noqa: B008
    notification_type: Annotated[NotificationType | None, Query()] = None,
    severity: Annotated[NotificationSeverity | None, Query()] = None,
    symbol: Annotated[str | None, Query()] = None,
    unread_only: Annotated[bool, Query()] = False,
) -> NotificationListResponse:
    normalized_symbol = symbol.strip().upper() if symbol is not None else None
    notifications = service.list_notifications(
        notification_type=notification_type,
        severity=severity,
        symbol=normalized_symbol,
        unread_only=unread_only,
    )
    return NotificationListResponse(count=len(notifications), notifications=notifications)


@router.get("/{notification_id}", response_model=NotificationResponse)
async def get_notification(
    notification_id: str,
    service: NotificationService = Depends(get_notification_service),  # noqa: B008
) -> NotificationResponse:
    notification = service.get_notification(notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    return notification


@router.post("/{notification_id}/read", response_model=NotificationResponse)
async def mark_as_read(
    notification_id: str,
    _authorization: MutationAuthorization = Depends(authorize_mutation),  # noqa: B008
    service: NotificationService = Depends(get_notification_service),  # noqa: B008
) -> NotificationResponse:
    """Idempotently mark one notification read behind the mutation boundary."""

    notification = service.mark_as_read(notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    return notification
