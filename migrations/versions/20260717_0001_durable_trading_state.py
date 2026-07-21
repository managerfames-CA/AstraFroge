"""Create durable Signal, Risk, execution intent, order, fill, position and trade tables.

Revision ID: 20260717_0001
Revises:
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260717_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signals",
        sa.Column("signal_id", sa.String(length=64), primary_key=True),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "signal_lifecycle_history",
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "signal_id",
            sa.String(length=64),
            sa.ForeignKey("signals.signal_id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("audit_code", sa.String(length=128), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("signal_id", "version", name="uq_signal_lifecycle_version"),
    )
    op.create_index(
        "ix_signal_lifecycle_signal_changed",
        "signal_lifecycle_history",
        ["signal_id", "changed_at"],
    )
    op.create_table(
        "risk_decisions",
        sa.Column("decision_id", sa.String(length=64), primary_key=True),
        sa.Column("signal_id", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("audit_codes_json", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_risk_signal_assessed",
        "risk_decisions",
        ["signal_id", "assessed_at"],
    )
    op.create_table(
        "execution_intents",
        sa.Column("intent_id", sa.String(length=64), primary_key=True),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("signal_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("client_order_ids_json", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "operation",
            "subject_id",
            name="uq_execution_intent_operation_subject",
        ),
    )
    op.create_index(
        "ix_execution_intent_state_updated",
        "execution_intents",
        ["state", "updated_at"],
    )
    op.create_table(
        "exchange_orders",
        sa.Column("order_id", sa.String(length=64), primary_key=True),
        sa.Column("signal_id", sa.String(length=64), nullable=True),
        sa.Column("trade_id", sa.String(length=64), nullable=True),
        sa.Column("client_order_id", sa.String(length=128), nullable=False),
        sa.Column("exchange_order_id", sa.String(length=128), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("quantity_text", sa.String(length=128), nullable=True),
        sa.Column("average_price_text", sa.String(length=128), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("client_order_id", name="uq_exchange_order_client_id"),
        sa.UniqueConstraint("exchange_order_id", name="uq_exchange_order_exchange_id"),
    )
    op.create_table(
        "fills",
        sa.Column("fill_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "order_id",
            sa.String(length=64),
            sa.ForeignKey("exchange_orders.order_id"),
            nullable=False,
        ),
        sa.Column("exchange_trade_id", sa.String(length=128), nullable=True),
        sa.Column("quantity_text", sa.String(length=128), nullable=False),
        sa.Column("price_text", sa.String(length=128), nullable=False),
        sa.Column("commission_text", sa.String(length=128), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("exchange_trade_id", name="uq_fill_exchange_trade_id"),
    )
    op.create_index("ix_fill_order_time", "fills", ["order_id", "filled_at"])
    op.create_table(
        "positions",
        sa.Column("position_id", sa.String(length=64), primary_key=True),
        sa.Column("account_scope", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("quantity_text", sa.String(length=128), nullable=False),
        sa.Column("entry_price_text", sa.String(length=128), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_scope", "symbol", name="uq_position_scope_symbol"),
    )
    op.create_table(
        "trades",
        sa.Column("trade_id", sa.String(length=64), primary_key=True),
        sa.Column("signal_id", sa.String(length=64), nullable=False),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("quantity_text", sa.String(length=128), nullable=False),
        sa.Column("entry_price_text", sa.String(length=128), nullable=False),
        sa.Column("exit_price_text", sa.String(length=128), nullable=True),
        sa.Column("realized_pnl_text", sa.String(length=128), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("signal_id", name="uq_trade_signal_id"),
    )


def downgrade() -> None:
    op.drop_table("trades")
    op.drop_table("positions")
    op.drop_index("ix_fill_order_time", table_name="fills")
    op.drop_table("fills")
    op.drop_table("exchange_orders")
    op.drop_index("ix_execution_intent_state_updated", table_name="execution_intents")
    op.drop_table("execution_intents")
    op.drop_index("ix_risk_signal_assessed", table_name="risk_decisions")
    op.drop_table("risk_decisions")
    op.drop_index(
        "ix_signal_lifecycle_signal_changed",
        table_name="signal_lifecycle_history",
    )
    op.drop_table("signal_lifecycle_history")
    op.drop_table("signals")
