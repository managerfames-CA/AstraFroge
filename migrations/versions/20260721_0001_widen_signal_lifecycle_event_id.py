"""Widen signal lifecycle event_id length.

Revision ID: 20260721_0001
Revises: 20260720_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260721_0001"
down_revision: str | None = "20260720_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("signal_lifecycle_history") as batch_op:
        batch_op.alter_column(
            "event_id",
            type_=sa.String(length=128),
            existing_type=sa.String(length=64),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("signal_lifecycle_history") as batch_op:
        batch_op.alter_column(
            "event_id",
            type_=sa.String(length=64),
            existing_type=sa.String(length=128),
            existing_nullable=False,
        )
