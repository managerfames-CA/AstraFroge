"""Make Binance user-trade identity symbol-scoped.

Revision ID: 20260720_0001
Revises: 20260719_0002
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260720_0001"
down_revision = "20260719_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add columns to fills table (making them nullable at first so we can populate them)
    with op.batch_alter_table("fills") as batch_op:
        batch_op.add_column(
            sa.Column(
                "account_scope",
                sa.String(length=32),
                nullable=False,
                server_default="BINANCE_DEMO",
            )
        )
        batch_op.add_column(
            sa.Column("symbol", sa.String(length=32), nullable=True)
        )

    # 2. Update existing records in fills to populate symbol from parent exchange_orders
    op.execute(
        "UPDATE fills SET symbol = (SELECT symbol FROM exchange_orders "
        "WHERE exchange_orders.order_id = fills.order_id)"
    )

    # Fallback default for symbol in case parent order is missing (should not happen, but safe)
    op.execute("UPDATE fills SET symbol = 'BTCUSDT' WHERE symbol IS NULL")

    # 3. Now alter column symbol to be non-nullable, drop old unique constraint,
    # and add new composite unique constraint
    with op.batch_alter_table("fills") as batch_op:
        batch_op.alter_column("symbol", nullable=False, existing_type=sa.String(length=32))
        batch_op.drop_constraint("uq_fill_exchange_trade_id", type_="unique")
        batch_op.create_unique_constraint(
            "uq_fill_exchange_trade_id",
            ["account_scope", "symbol", "exchange_trade_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("fills") as batch_op:
        batch_op.drop_constraint("uq_fill_exchange_trade_id", type_="unique")
        batch_op.create_unique_constraint(
            "uq_fill_exchange_trade_id", ["exchange_trade_id"]
        )
        batch_op.drop_column("symbol")
        batch_op.drop_column("account_scope")
