"""SQLAlchemy schema for durable authoritative trading state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime


class Base(DeclarativeBase):
    """Declarative persistence metadata."""


class SignalRow(Base):
    __tablename__ = "signals"

    signal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    history: Mapped[list[SignalLifecycleRow]] = relationship(
        back_populates="signal", cascade="all, delete-orphan"
    )


class SignalLifecycleRow(Base):
    __tablename__ = "signal_lifecycle_history"
    __table_args__ = (
        UniqueConstraint("signal_id", "version", name="uq_signal_lifecycle_version"),
        Index("ix_signal_lifecycle_signal_changed", "signal_id", "changed_at"),
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    signal_id: Mapped[str] = mapped_column(ForeignKey("signals.signal_id"), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    audit_code: Mapped[str | None] = mapped_column(String(128))
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    signal: Mapped[SignalRow] = relationship(back_populates="history")


class RiskDecisionRow(Base):
    __tablename__ = "risk_decisions"
    __table_args__ = (Index("ix_risk_signal_assessed", "signal_id", "assessed_at"),)

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    audit_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionIntentRow(Base):
    __tablename__ = "execution_intents"
    __table_args__ = (
        UniqueConstraint("operation", "subject_id", name="uq_execution_intent_operation_subject"),
        Index("ix_execution_intent_state_updated", "state", "updated_at"),
    )

    intent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    signal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    client_order_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionCommandRow(Base):
    __tablename__ = "execution_commands"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_execution_command_idempotency"),
        UniqueConstraint(
            "signal_id",
            "decision_key",
            "source_snapshot_version",
            name="uq_execution_command_signal_decision_snapshot",
        ),
        Index("ix_execution_command_state_created", "state", "created_at"),
        Index("ix_execution_command_expiry", "expires_at"),
        Index("ix_execution_command_signal_updated", "signal_id", "updated_at"),
    )

    command_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    signal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_key: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_decision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot_version: Mapped[str] = mapped_column(String(256), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    claim_token: Mapped[str | None] = mapped_column(String(64))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(128))
    entry_exchange_order_id: Mapped[str | None] = mapped_column(String(128))
    stop_exchange_order_id: Mapped[str | None] = mapped_column(String(128))
    take_profit_exchange_order_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    history: Mapped[list[ExecutionCommandTransitionRow]] = relationship(
        back_populates="command", cascade="all, delete-orphan"
    )


class ExecutionCommandTransitionRow(Base):
    __tablename__ = "execution_command_transitions"
    __table_args__ = (
        UniqueConstraint(
            "command_id", "sequence", name="uq_execution_command_transition_sequence"
        ),
        Index(
            "ix_execution_command_transition_changed",
            "command_id",
            "changed_at",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("execution_commands.command_id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    command: Mapped[ExecutionCommandRow] = relationship(back_populates="history")


class ExchangeOrderRow(Base):
    __tablename__ = "exchange_orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_exchange_order_client_id"),
        UniqueConstraint("exchange_order_id", name="uq_exchange_order_exchange_id"),
    )

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    signal_id: Mapped[str | None] = mapped_column(String(64))
    trade_id: Mapped[str | None] = mapped_column(String(64))
    client_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(128))
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity_text: Mapped[str | None] = mapped_column(String(128))
    average_price_text: Mapped[str | None] = mapped_column(String(128))
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fills: Mapped[list[FillRow]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class FillRow(Base):
    __tablename__ = "fills"
    __table_args__ = (
        UniqueConstraint(
            "account_scope",
            "symbol",
            "exchange_trade_id",
            name="uq_fill_exchange_trade_id",
        ),
        Index("ix_fill_order_time", "order_id", "filled_at"),
    )

    fill_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("exchange_orders.order_id"), nullable=False)
    account_scope: Mapped[str] = mapped_column(String(32), nullable=False, default="BINANCE_DEMO")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange_trade_id: Mapped[str | None] = mapped_column(String(128))
    quantity_text: Mapped[str] = mapped_column(String(128), nullable=False)
    price_text: Mapped[str] = mapped_column(String(128), nullable=False)
    commission_text: Mapped[str | None] = mapped_column(String(128))
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    order: Mapped[ExchangeOrderRow] = relationship(back_populates="fills")


class PositionRow(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("account_scope", "symbol", name="uq_position_scope_symbol"),)

    position_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_scope: Mapped[str] = mapped_column(String(32), nullable=False, default="BINANCE_DEMO")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity_text: Mapped[str] = mapped_column(String(128), nullable=False)
    entry_price_text: Mapped[str | None] = mapped_column(String(128))
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TradeRow(Base):
    __tablename__ = "trades"
    __table_args__ = (UniqueConstraint("signal_id", name="uq_trade_signal_id"),)

    trade_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity_text: Mapped[str] = mapped_column(String(128), nullable=False)
    entry_price_text: Mapped[str] = mapped_column(String(128), nullable=False)
    exit_price_text: Mapped[str | None] = mapped_column(String(128))
    realized_pnl_text: Mapped[str | None] = mapped_column(String(128))
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MutationReplayKeyRow(Base):
    __tablename__ = "mutation_replay_keys"
    __table_args__ = (Index("ix_mutation_replay_expires", "expires_at"),)

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
