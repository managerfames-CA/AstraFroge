"""Create durable mutation replay registry.

Revision ID: 20260717_0002
Revises: 20260717_0001
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260717_0002"
down_revision = "20260717_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mutation_replay_keys",
        sa.Column("key_hash", sa.String(length=64), primary_key=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_mutation_replay_expires",
        "mutation_replay_keys",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mutation_replay_expires", table_name="mutation_replay_keys")
    op.drop_table("mutation_replay_keys")
