"""Add durable notifications table for BE-16.

Revision ID: 20260720_0002
Revises: 20260720_0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260720_0002"
down_revision: str | None = "20260720_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("notification_id", sa.String(length=64), nullable=False),
        sa.Column("deduplication_key", sa.String(length=256), nullable=False),
        sa.Column("notification_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_identity", sa.String(length=128), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("signal_id", sa.String(length=64), nullable=True),
        sa.Column("trade_id", sa.String(length=64), nullable=True),
        sa.Column("order_id", sa.String(length=128), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivery_state", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("notification_id"),
        sa.UniqueConstraint("deduplication_key", name="uq_notification_deduplication"),
    )
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])
    op.create_index(
        "ix_notifications_type_severity",
        "notifications",
        ["notification_type", "severity"],
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_type_severity", table_name="notifications")
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_table("notifications")
