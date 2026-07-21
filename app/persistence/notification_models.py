"""SQLAlchemy model for durable BE-16 notifications."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.persistence.models import Base


class NotificationRow(Base):
    """Durable, deduplicated notification record."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("deduplication_key", name="uq_notification_deduplication"),
        Index("ix_notifications_created_at", "created_at"),
        Index("ix_notifications_type_severity", "notification_type", "severity"),
    )

    notification_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    deduplication_key: Mapped[str] = mapped_column(String(256), nullable=False)
    notification_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_identity: Mapped[str | None] = mapped_column(String(128))
    symbol: Mapped[str | None] = mapped_column(String(32))
    signal_id: Mapped[str | None] = mapped_column(String(64))
    trade_id: Mapped[str | None] = mapped_column(String(64))
    order_id: Mapped[str | None] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(32), nullable=False)
