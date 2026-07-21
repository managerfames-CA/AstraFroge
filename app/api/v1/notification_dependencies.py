"""Dependency factory for the durable BE-16 notification service."""

from __future__ import annotations

from app.api.v1 import dependencies as runtime_dependencies
from app.services.notifications import NotificationService


def get_notification_service() -> NotificationService:
    """Build the notification service from the app-scoped persistence authority."""

    repositories = runtime_dependencies._runtime_repositories
    if repositories is None:
        raise RuntimeError("Durable notification persistence is not configured")
    return NotificationService(repositories.persistence)
