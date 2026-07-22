"""Add durable Phase 5 execution commands and transition history.

Revision ID: 20260719_0002
Revises: 20260717_0002
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260719_0002"
down_revision = "20260717_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_commands",
        sa.Column("command_id", sa.String(length=64), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("signal_id", sa.String(length=64), nullable=False),
        sa.Column("decision_key", sa.String(length=64), nullable=False),
        sa.Column("risk_decision_id", sa.String(length=64), nullable=False),
        sa.Column("source_snapshot_version", sa.String(length=256), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(length=128), nullable=True),
        sa.Column("entry_exchange_order_id", sa.String(length=128), nullable=True),
        sa.Column("stop_exchange_order_id", sa.String(length=128), nullable=True),
        sa.Column("take_profit_exchange_order_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_execution_command_idempotency"),
        sa.UniqueConstraint(
            "signal_id",
            "decision_key",
            "source_snapshot_version",
            name="uq_execution_command_signal_decision_snapshot",
        ),
    )
    op.create_index(
        "ix_execution_command_state_created",
        "execution_commands",
        ["state", "created_at"],
    )
    op.create_index(
        "ix_execution_command_expiry",
        "execution_commands",
        ["expires_at"],
    )
    op.create_index(
        "ix_execution_command_signal_updated",
        "execution_commands",
        ["signal_id", "updated_at"],
    )
    op.create_table(
        "execution_command_transitions",
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "command_id",
            sa.String(length=64),
            sa.ForeignKey("execution_commands.command_id"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "command_id",
            "sequence",
            name="uq_execution_command_transition_sequence",
        ),
    )
    op.create_index(
        "ix_execution_command_transition_changed",
        "execution_command_transitions",
        ["command_id", "changed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_command_transition_changed",
        table_name="execution_command_transitions",
    )
    op.drop_table("execution_command_transitions")
    op.drop_index(
        "ix_execution_command_signal_updated",
        table_name="execution_commands",
    )
    op.drop_index("ix_execution_command_expiry", table_name="execution_commands")
    op.drop_index(
        "ix_execution_command_state_created",
        table_name="execution_commands",
    )
    op.drop_table("execution_commands")
