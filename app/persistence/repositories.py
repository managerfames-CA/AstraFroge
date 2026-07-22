"""Explicit repositories for durable trading-state records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.persistence.database import Persistence
from app.persistence.models import (
    ExchangeOrderRow,
    FillRow,
    MutationReplayKeyRow,
    PositionRow,
    RiskDecisionRow,
    SignalLifecycleRow,
    SignalRow,
    TradeRow,
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Persistence timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _json(payload: dict[str, Any] | list[str]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if not value.is_finite():
        raise ValueError("Financial values must be finite Decimals")
    return format(value, "f")


class TradingStateRepositories:
    """Central repository facade; callers may share one atomic transaction."""

    def __init__(self, persistence: Persistence) -> None:
        self.persistence = persistence

    def save_signal(
        self,
        *,
        signal_id: str,
        lifecycle: str,
        payload: dict[str, Any],
        created_at: datetime,
        updated_at: datetime,
        session: Session | None = None,
    ) -> bool:
        return self._run(
            session,
            lambda db: self._insert_if_missing(
                db,
                SignalRow(
                    signal_id=signal_id,
                    lifecycle=lifecycle,
                    payload_json=_json(payload),
                    created_at=_utc(created_at),
                    updated_at=_utc(updated_at),
                ),
                SignalRow,
                signal_id,
            ),
        )

    def append_signal_lifecycle(
        self,
        *,
        event_id: str,
        signal_id: str,
        version: int,
        lifecycle: str,
        audit_code: str | None,
        payload: dict[str, Any],
        changed_at: datetime,
        session: Session | None = None,
    ) -> bool:
        return self._run(
            session,
            lambda db: self._insert_if_missing(
                db,
                SignalLifecycleRow(
                    event_id=event_id,
                    signal_id=signal_id,
                    version=version,
                    lifecycle=lifecycle,
                    audit_code=audit_code,
                    payload_json=_json(payload),
                    changed_at=_utc(changed_at),
                ),
                SignalLifecycleRow,
                event_id,
            ),
        )

    def save_risk_decision(
        self,
        *,
        decision_id: str,
        signal_id: str,
        decision: str,
        audit_codes: list[str],
        payload: dict[str, Any],
        assessed_at: datetime,
        session: Session | None = None,
    ) -> bool:
        return self._run(
            session,
            lambda db: self._insert_if_missing(
                db,
                RiskDecisionRow(
                    decision_id=decision_id,
                    signal_id=signal_id,
                    decision=decision,
                    audit_codes_json=_json(audit_codes),
                    payload_json=_json(payload),
                    assessed_at=_utc(assessed_at),
                ),
                RiskDecisionRow,
                decision_id,
            ),
        )

    def save_order(
        self,
        *,
        order_id: str,
        client_order_id: str,
        symbol: str,
        status: str,
        payload: dict[str, Any],
        created_at: datetime,
        updated_at: datetime,
        signal_id: str | None = None,
        trade_id: str | None = None,
        exchange_order_id: str | None = None,
        quantity: Decimal | None = None,
        average_price: Decimal | None = None,
        session: Session | None = None,
    ) -> bool:
        return self._run(
            session,
            lambda db: self._insert_if_missing(
                db,
                ExchangeOrderRow(
                    order_id=order_id,
                    signal_id=signal_id,
                    trade_id=trade_id,
                    client_order_id=client_order_id,
                    exchange_order_id=exchange_order_id,
                    symbol=symbol,
                    status=status,
                    quantity_text=_decimal(quantity),
                    average_price_text=_decimal(average_price),
                    payload_json=_json(payload),
                    created_at=_utc(created_at),
                    updated_at=_utc(updated_at),
                ),
                ExchangeOrderRow,
                order_id,
            ),
        )

    def save_fill(
        self,
        *,
        fill_id: str,
        order_id: str,
        quantity: Decimal,
        price: Decimal,
        payload: dict[str, Any],
        filled_at: datetime,
        exchange_trade_id: str | None = None,
        commission: Decimal | None = None,
        session: Session | None = None,
        symbol: str | None = None,
        account_scope: str = "BINANCE_DEMO",
    ) -> bool:
        def do_save(db: Session) -> bool:
            nonlocal symbol
            if symbol is None:
                order = db.get(ExchangeOrderRow, order_id)
                symbol = order.symbol if order is not None else "BTCUSDT"
            return self._insert_if_missing(
                db,
                FillRow(
                    fill_id=fill_id,
                    order_id=order_id,
                    account_scope=account_scope,
                    symbol=symbol,
                    exchange_trade_id=exchange_trade_id,
                    quantity_text=_decimal(quantity) or "0",
                    price_text=_decimal(price) or "0",
                    commission_text=_decimal(commission),
                    payload_json=_json(payload),
                    filled_at=_utc(filled_at),
                ),
                FillRow,
                fill_id,
            )

        return self._run(session, do_save)

    def save_position(
        self,
        *,
        position_id: str,
        symbol: str,
        quantity: Decimal,
        payload: dict[str, Any],
        captured_at: datetime,
        updated_at: datetime,
        entry_price: Decimal | None = None,
        account_scope: str = "BINANCE_DEMO",
        session: Session | None = None,
    ) -> bool:
        return self._run(
            session,
            lambda db: self._insert_if_missing(
                db,
                PositionRow(
                    position_id=position_id,
                    account_scope=account_scope,
                    symbol=symbol,
                    quantity_text=_decimal(quantity) or "0",
                    entry_price_text=_decimal(entry_price),
                    payload_json=_json(payload),
                    captured_at=_utc(captured_at),
                    updated_at=_utc(updated_at),
                ),
                PositionRow,
                position_id,
            ),
        )

    def save_trade(
        self,
        *,
        trade_id: str,
        signal_id: str,
        lifecycle: str,
        symbol: str,
        quantity: Decimal,
        entry_price: Decimal,
        payload: dict[str, Any],
        opened_at: datetime,
        updated_at: datetime,
        exit_price: Decimal | None = None,
        realized_pnl: Decimal | None = None,
        closed_at: datetime | None = None,
        session: Session | None = None,
    ) -> bool:
        return self._run(
            session,
            lambda db: self._insert_if_missing(
                db,
                TradeRow(
                    trade_id=trade_id,
                    signal_id=signal_id,
                    lifecycle=lifecycle,
                    symbol=symbol,
                    quantity_text=_decimal(quantity) or "0",
                    entry_price_text=_decimal(entry_price) or "0",
                    exit_price_text=_decimal(exit_price),
                    realized_pnl_text=_decimal(realized_pnl),
                    payload_json=_json(payload),
                    opened_at=_utc(opened_at),
                    closed_at=_utc(closed_at) if closed_at is not None else None,
                    updated_at=_utc(updated_at),
                ),
                TradeRow,
                trade_id,
            ),
        )

    def signal(self, signal_id: str) -> SignalRow | None:
        with self.persistence.transaction() as session:
            return session.get(SignalRow, signal_id)

    def signal_history(self, signal_id: str) -> list[SignalLifecycleRow]:
        with self.persistence.transaction() as session:
            statement = (
                select(SignalLifecycleRow)
                .where(SignalLifecycleRow.signal_id == signal_id)
                .order_by(SignalLifecycleRow.version)
            )
            return list(session.scalars(statement))

    def order_with_fills(self, order_id: str) -> ExchangeOrderRow | None:
        with self.persistence.transaction() as session:
            order = session.get(ExchangeOrderRow, order_id)
            if order is not None:
                _ = list(order.fills)
            return order

    def trade(self, trade_id: str) -> TradeRow | None:
        with self.persistence.transaction() as session:
            return session.get(TradeRow, trade_id)

    def active_mutation_replay(
        self,
        key_hash: str,
        *,
        now: datetime,
    ) -> MutationReplayKeyRow | None:
        with self.persistence.transaction() as session:
            row = session.get(MutationReplayKeyRow, key_hash)
            if row is None or _stored_utc(row.expires_at) <= _utc(now):
                return None
            return row

    def claim_mutation_replay(
        self,
        *,
        key_hash: str,
        fingerprint: str,
        action: str,
        now: datetime,
        expires_at: datetime,
        cache_limit: int,
    ) -> tuple[bool, MutationReplayKeyRow | None]:
        current = _utc(now)
        expiry = _utc(expires_at)
        with self.persistence.transaction() as session:
            self._prune_expired_mutation_replays(session, now=current)

            existing = session.get(MutationReplayKeyRow, key_hash)
            if existing is not None:
                return False, existing

            if self._active_mutation_replay_count(session, now=current) >= cache_limit:
                return False, None

            inserted = self._insert_if_missing(
                session,
                MutationReplayKeyRow(
                    key_hash=key_hash,
                    fingerprint=fingerprint,
                    action=action,
                    claimed_at=current,
                    expires_at=expiry,
                ),
                MutationReplayKeyRow,
                key_hash,
            )
            if inserted:
                return True, session.get(MutationReplayKeyRow, key_hash)

            recycled = self._replace_expired_mutation_replay(
                session,
                key_hash=key_hash,
                fingerprint=fingerprint,
                action=action,
                now=current,
                expires_at=expiry,
            )
            if recycled:
                row = session.get(MutationReplayKeyRow, key_hash)
                return True, row

            row = session.get(MutationReplayKeyRow, key_hash)
            return False, row

    @staticmethod
    def _insert_if_missing(
        session: Session,
        row: Any,
        model: type[Any],
        stable_id: str,
    ) -> bool:
        if session.get(model, stable_id) is not None:
            return False
        values = {column.name: getattr(row, column.name) for column in row.__table__.columns}
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "postgresql":
            result = cast(
                CursorResult[Any],
                session.execute(
                    postgresql_insert(row.__table__).values(**values).on_conflict_do_nothing()
                ),
            )
            return result.rowcount > 0
        if dialect_name == "sqlite":
            result = cast(
                CursorResult[Any],
                session.execute(
                    sqlite_insert(row.__table__).values(**values).on_conflict_do_nothing()
                ),
            )
            return result.rowcount > 0

        session.add(row)
        session.flush()
        return True

    def _run(self, session: Session | None, operation: Any) -> bool:
        if session is not None:
            return bool(operation(session))
        with self.persistence.transaction() as owned:
            return bool(operation(owned))

    @staticmethod
    def _active_mutation_replay_count(session: Session, *, now: datetime) -> int:
        statement = (
            select(func.count())
            .select_from(MutationReplayKeyRow)
            .where(MutationReplayKeyRow.expires_at > now)
        )
        return int(session.scalar(statement) or 0)

    @staticmethod
    def _prune_expired_mutation_replays(session: Session, *, now: datetime) -> None:
        statement = delete(MutationReplayKeyRow).where(MutationReplayKeyRow.expires_at <= now)
        session.execute(statement)

    @staticmethod
    def _replace_expired_mutation_replay(
        session: Session,
        *,
        key_hash: str,
        fingerprint: str,
        action: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        statement = (
            update(MutationReplayKeyRow)
            .where(MutationReplayKeyRow.key_hash == key_hash)
            .where(MutationReplayKeyRow.expires_at <= now)
            .values(
                fingerprint=fingerprint,
                action=action,
                claimed_at=now,
                expires_at=expires_at,
            )
        )
        result = cast(CursorResult[Any], session.execute(statement))
        return result.rowcount > 0
