"""Durable, restart-safe and multi-instance-safe manual close state authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.persistence.models import ExchangeOrderRow, ExecutionIntentRow
from app.persistence.repositories import TradingStateRepositories
from app.schemas.execution import DemoTradeLifecycle, DemoTradeRecord
from app.schemas.trade_management import (
    ManualCloseIntentSnapshot,
    ManualCloseIntentState,
)


class ManualCloseDurabilityError(RuntimeError):
    """Durable close state is missing, conflicting or malformed."""


class ManualCloseDurabilityService:
    """Persist one deterministic manual-close lifecycle before and after exchange I/O."""

    def __init__(
        self,
        repositories: TradingStateRepositories,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._repositories = repositories
        self._now = now_provider or (lambda: datetime.now(UTC))

    @staticmethod
    def intent_id(trade_id: str) -> str:
        """Return the stable close-intent identity for one durable trade."""

        return hashlib.sha256(f"CLOSE:{trade_id}".encode()).hexdigest()

    @staticmethod
    def order_row_id(trade_id: str) -> str:
        """Return a PostgreSQL-safe 64-character identity for the close order row."""

        return hashlib.sha256(f"MANUAL_CLOSE_ORDER:{trade_id}".encode()).hexdigest()

    def prepare(
        self,
        trade: DemoTradeRecord,
        client_order_id: str,
    ) -> ManualCloseIntentSnapshot:
        """Create the intent before exchange mutation or load its existing outcome."""

        intent_id = self.intent_id(trade.trade_id)
        now = self._now()
        try:
            with self._repositories.persistence.transaction() as session:
                row = session.scalar(
                    select(ExecutionIntentRow)
                    .where(ExecutionIntentRow.intent_id == intent_id)
                    .with_for_update()
                )
                if row is None:
                    row = ExecutionIntentRow(
                        intent_id=intent_id,
                        operation="CLOSE",
                        subject_id=trade.trade_id,
                        signal_id=trade.signal_id,
                        state=ManualCloseIntentState.PENDING.value,
                        client_order_ids_json=self._json([client_order_id]),
                        payload_json=self._json(
                            {
                                "requested_operation": "CLOSE_DEMO_TRADE",
                                "trade_id": trade.trade_id,
                                "client_order_id": client_order_id,
                            }
                        ),
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    session.flush()
                self._validate_identity(row, trade, client_order_id)
                return self._snapshot(row)
        except IntegrityError as exc:
            with self._repositories.persistence.transaction() as session:
                row = session.get(ExecutionIntentRow, intent_id)
                if row is None:
                    raise ManualCloseDurabilityError(
                        "Durable manual-close intent could not be recovered"
                    ) from exc
                self._validate_identity(row, trade, client_order_id)
                return self._snapshot(row)

    def record_exchange_result(
        self,
        trade: DemoTradeRecord,
        client_order_id: str,
        payload: dict[str, Any],
    ) -> ManualCloseIntentSnapshot:
        """Persist exchange order identity and SUBMITTED/FILLED state immediately."""

        exchange_client_id = self._text(payload.get("clientOrderId"))
        exchange_order_id = self._text(payload.get("orderId"))
        status = self._text(payload.get("status"))
        if exchange_client_id != client_order_id or exchange_order_id is None or status is None:
            raise ManualCloseDurabilityError("Manual close exchange identity is invalid")

        quantity = self._optional_decimal(payload.get("executedQty"))
        average_price = self._optional_decimal(payload.get("avgPrice"))
        if status == "FILLED" and (
            quantity is None
            or quantity <= 0
            or average_price is None
            or average_price <= 0
        ):
            raise ManualCloseDurabilityError("Filled manual close economics are invalid")

        intent_id = self.intent_id(trade.trade_id)
        now = self._now()
        state = (
            ManualCloseIntentState.FILLED
            if status == "FILLED"
            else ManualCloseIntentState.SUBMITTED
        )
        with self._repositories.persistence.transaction() as session:
            intent = session.scalar(
                select(ExecutionIntentRow)
                .where(ExecutionIntentRow.intent_id == intent_id)
                .with_for_update()
            )
            if intent is None:
                raise ManualCloseDurabilityError("Durable manual-close intent is missing")
            self._validate_identity(intent, trade, client_order_id)
            if intent.state == ManualCloseIntentState.COMPLETED.value:
                return self._snapshot(intent)

            order_id = self.order_row_id(trade.trade_id)
            order = session.get(ExchangeOrderRow, order_id)
            if order is None:
                order = ExchangeOrderRow(
                    order_id=order_id,
                    signal_id=trade.signal_id,
                    trade_id=trade.trade_id,
                    client_order_id=client_order_id,
                    exchange_order_id=exchange_order_id,
                    symbol=trade.symbol,
                    status=status,
                    quantity_text=self._decimal_text(quantity),
                    average_price_text=self._decimal_text(average_price),
                    payload_json=self._json(
                        {
                            "source": "verified_manual_close_order",
                            "status": status,
                        }
                    ),
                    created_at=now,
                    updated_at=now,
                )
                session.add(order)
            else:
                if order.client_order_id != client_order_id:
                    raise ManualCloseDurabilityError(
                        "Manual close client-order identity changed"
                    )
                if order.exchange_order_id not in {None, exchange_order_id}:
                    raise ManualCloseDurabilityError(
                        "Manual close exchange-order identity changed"
                    )
                order.exchange_order_id = exchange_order_id
                order.status = status
                order.quantity_text = self._decimal_text(quantity)
                order.average_price_text = self._decimal_text(average_price)
                order.payload_json = self._json(
                    {
                        "source": "verified_manual_close_order",
                        "status": status,
                    }
                )
                order.updated_at = now

            merged = self._payload(intent)
            merged.update(
                {
                    "trade_id": trade.trade_id,
                    "client_order_id": client_order_id,
                    "exchange_order_id": exchange_order_id,
                    "order_status": status,
                    "executed_quantity": self._decimal_text(quantity),
                    "average_fill_price": self._decimal_text(average_price),
                }
            )
            intent.state = state.value
            intent.payload_json = self._json(merged)
            intent.updated_at = now
            session.flush()
            return self._snapshot(intent)

    def complete(
        self,
        trade: DemoTradeRecord,
        client_order_id: str,
    ) -> ManualCloseIntentSnapshot:
        """Persist the first authoritative final trade for deterministic replay."""

        if trade.lifecycle is not DemoTradeLifecycle.CLOSED or trade.closed_at is None:
            raise ManualCloseDurabilityError("Manual close cannot complete with an open trade")
        intent_id = self.intent_id(trade.trade_id)
        now = self._now()
        with self._repositories.persistence.transaction() as session:
            intent = session.scalar(
                select(ExecutionIntentRow)
                .where(ExecutionIntentRow.intent_id == intent_id)
                .with_for_update()
            )
            if intent is None:
                raise ManualCloseDurabilityError("Durable manual-close intent is missing")
            self._validate_identity(intent, trade, client_order_id)
            if intent.state == ManualCloseIntentState.COMPLETED.value:
                return self._snapshot(intent)
            merged = self._payload(intent)
            merged.update(
                {
                    "trade_id": trade.trade_id,
                    "client_order_id": client_order_id,
                    "closed_at": trade.closed_at.isoformat(),
                    "completed_trade": trade.model_dump(mode="json"),
                }
            )
            intent.state = ManualCloseIntentState.COMPLETED.value
            intent.payload_json = self._json(merged)
            intent.updated_at = now
            session.flush()
            return self._snapshot(intent)

    def mark_recovery_required(
        self,
        trade: DemoTradeRecord,
        client_order_id: str,
        reason: str,
    ) -> ManualCloseIntentSnapshot:
        """Preserve all known exchange evidence and mark finalization for retry."""

        intent_id = self.intent_id(trade.trade_id)
        now = self._now()
        with self._repositories.persistence.transaction() as session:
            intent = session.scalar(
                select(ExecutionIntentRow)
                .where(ExecutionIntentRow.intent_id == intent_id)
                .with_for_update()
            )
            if intent is None:
                raise ManualCloseDurabilityError("Durable manual-close intent is missing")
            self._validate_identity(intent, trade, client_order_id)
            if intent.state == ManualCloseIntentState.COMPLETED.value:
                return self._snapshot(intent)
            merged = self._payload(intent)
            merged["recovery_reason"] = reason
            intent.state = ManualCloseIntentState.RECOVERY_REQUIRED.value
            intent.payload_json = self._json(merged)
            intent.updated_at = now
            session.flush()
            return self._snapshot(intent)

    def _snapshot(self, row: ExecutionIntentRow) -> ManualCloseIntentSnapshot:
        payload = self._payload(row)
        client_ids = json.loads(row.client_order_ids_json)
        if not isinstance(client_ids, list) or len(client_ids) != 1:
            raise ManualCloseDurabilityError("Manual close client-order identity is invalid")
        completed_payload = payload.get("completed_trade")
        completed_trade = (
            DemoTradeRecord.model_validate(completed_payload)
            if isinstance(completed_payload, dict)
            else None
        )
        try:
            state = ManualCloseIntentState(row.state)
        except ValueError as exc:
            raise ManualCloseDurabilityError("Manual close intent state is invalid") from exc
        return ManualCloseIntentSnapshot(
            intent_id=row.intent_id,
            trade_id=row.subject_id,
            client_order_id=str(client_ids[0]),
            state=state,
            exchange_order_id=self._text(payload.get("exchange_order_id")),
            order_status=self._text(payload.get("order_status")),
            completed_trade=completed_trade,
            updated_at=self._aware(row.updated_at),
        )

    @staticmethod
    def _validate_identity(
        row: ExecutionIntentRow,
        trade: DemoTradeRecord,
        client_order_id: str,
    ) -> None:
        try:
            client_ids = json.loads(row.client_order_ids_json)
        except json.JSONDecodeError as exc:
            raise ManualCloseDurabilityError(
                "Manual close client-order identity is malformed"
            ) from exc
        if (
            row.operation != "CLOSE"
            or row.subject_id != trade.trade_id
            or row.signal_id != trade.signal_id
            or client_ids != [client_order_id]
        ):
            raise ManualCloseDurabilityError("Manual close durable identity conflict")

    @staticmethod
    def _payload(row: ExecutionIntentRow) -> dict[str, Any]:
        try:
            payload = json.loads(row.payload_json)
        except json.JSONDecodeError as exc:
            raise ManualCloseDurabilityError("Manual close payload is malformed") from exc
        if not isinstance(payload, dict):
            raise ManualCloseDurabilityError("Manual close payload is invalid")
        return payload

    @staticmethod
    def _json(payload: dict[str, Any] | list[str]) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _optional_decimal(value: Any) -> Decimal | None:
        if value in {None, ""}:
            return None
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ManualCloseDurabilityError(
                "Manual close exchange decimal is invalid"
            ) from exc
        if not parsed.is_finite() or parsed < 0:
            raise ManualCloseDurabilityError(
                "Manual close exchange decimal is invalid"
            )
        return parsed

    @staticmethod
    def _decimal_text(value: Decimal | None) -> str | None:
        return format(value, "f") if value is not None else None

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
