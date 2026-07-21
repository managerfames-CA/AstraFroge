"""Runtime-safe BE-14 order-audit integration and compatibility rules."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import select

from app.persistence.models import ExchangeOrderRow, FillRow
from app.persistence.repositories import TradingStateRepositories
from app.schemas.execution import DemoTradeCloseReason, DemoTradeRecord
from app.schemas.order_audit import OrderAuditRole
from app.services.order_audit import (
    _ALLOWED_STATUSES,
    OrderAuditClient,
    OrderAuditService,
    OrderAuditTradeSource,
    OrderAuditVerificationError,
    _Fill,
)
from app.services.protective_lifecycle import ProtectiveLifecycleVerificationService
from app.services.recovery import AutomationRecoveryGate

_TERMINAL_STATUSES = frozenset(
    {"FILLED", "FINISHED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
)
_PROTECTIVE_ROLES = frozenset({OrderAuditRole.STOP_LOSS, OrderAuditRole.TAKE_PROFIT})


class RuntimeOrderAuditService(OrderAuditService):
    """Order audit with production-compatible Binance lifecycle semantics."""

    @classmethod
    def from_protective_service(
        cls,
        service: ProtectiveLifecycleVerificationService | None,
        *,
        interval_seconds: float = 15.0,
    ) -> RuntimeOrderAuditService | None:
        """Reuse the app-scoped lifecycle authority's verified runtime dependencies."""

        if service is None:
            return None
        trade_source = getattr(service, "_trade_source", None)
        client = getattr(service, "_client", None)
        repositories = getattr(service, "_repositories", None)
        gate = getattr(service, "_gate", None)
        if trade_source is None or not isinstance(gate, AutomationRecoveryGate):
            return None
        return cls(
            cast(OrderAuditTradeSource, trade_source),
            cast(OrderAuditClient | None, client),
            cast(TradingStateRepositories | None, repositories),
            gate,
            interval_seconds=interval_seconds,
        )

    @classmethod
    def _persist_fill(cls, session: Any, order_id: str, fill: _Fill) -> None:
        """Persist symbol-scoped Binance trade identity under the legacy global key."""

        session.flush()
        order = session.get(ExchangeOrderRow, order_id)
        if order is None:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_FILL_ORDER_MISSING",
                "Durable order is missing while a verified fill is recorded",
            )
        existing = session.scalar(
            select(FillRow).where(
                FillRow.account_scope == "BINANCE_DEMO",
                FillRow.symbol == order.symbol,
                FillRow.exchange_trade_id == fill.exchange_trade_id,
            )
        )
        if existing is not None:
            if (
                existing.order_id != order_id
                or cls._positive_decimal(existing.quantity_text) != fill.quantity
                or cls._positive_decimal(existing.price_text) != fill.price
            ):
                raise OrderAuditVerificationError(
                    "ORDER_AUDIT_FILL_IDENTITY_CONFLICT",
                    "Symbol-scoped exchange fill identity conflicts with durable state",
                )
            return
        fill_key = f"BINANCE_DEMO:{order.symbol}:{fill.exchange_trade_id}"
        fill_id = hashlib.sha256(
            f"ORDER_AUDIT_FILL:{fill_key}".encode()
        ).hexdigest()
        session.add(
            FillRow(
                fill_id=fill_id,
                order_id=order_id,
                account_scope="BINANCE_DEMO",
                symbol=order.symbol,
                exchange_trade_id=fill.exchange_trade_id,
                quantity_text=cls._decimal_text(fill.quantity) or "0",
                price_text=cls._decimal_text(fill.price) or "0",
                commission_text=None,
                payload_json=cls._json(
                    {
                        "source": "verified_binance_user_trade",
                        "symbol": order.symbol,
                        "exchange_trade_id": fill.exchange_trade_id,
                    }
                ),
                filled_at=fill.filled_at,
            )
        )

    @classmethod
    def _validate_economics(
        cls,
        *,
        role: OrderAuditRole,
        requested: Decimal,
        executed: Decimal,
        average: Decimal | None,
        status: str,
        actual_order_id: str | None,
        trade: DemoTradeRecord | None = None,
    ) -> None:
        """Validate fills without assuming every FINISHED Algo was executed."""

        if status not in _ALLOWED_STATUSES:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_STATUS_INVALID",
                "Exchange order status is unsupported",
            )
        if executed > requested:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_OVERFILL",
                "Executed quantity exceeds requested quantity",
            )
        if executed > 0 and average is None:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_AVERAGE_PRICE_MISSING",
                "Executed order has no verified weighted-average fill price",
            )
        if executed == 0 and average is not None:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_AVERAGE_PRICE_WITHOUT_FILL",
                "Unfilled order has a non-zero average fill price",
            )
        if role in _PROTECTIVE_ROLES and executed > 0 and actual_order_id is None:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_ACTUAL_ORDER_ID_MISSING",
                "Executed protective Algo has no actual regular-order identity",
            )
        if status == "NEW" and executed != 0:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_NEW_STATUS_HAS_FILL",
                "NEW order has executed quantity",
            )
        if status == "PARTIALLY_FILLED" and not (
            Decimal("0") < executed < requested
        ):
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_PARTIAL_STATUS_INVALID",
                "PARTIALLY_FILLED economics are invalid",
            )
        if status == "FILLED" and executed != requested:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_TERMINAL_QUANTITY_INVALID",
                "FILLED order does not equal requested quantity",
            )
        if status == "FINISHED" and role in _PROTECTIVE_ROLES:
            if executed == 0:
                if average is not None:
                    raise OrderAuditVerificationError(
                        "ORDER_AUDIT_AVERAGE_PRICE_WITHOUT_FILL",
                        f"FINISHED protective sibling {role} has zero executed quantity "
                        "but has average price",
                    )
                if trade is not None:
                    opposite_reason = (
                        DemoTradeCloseReason.TAKE_PROFIT
                        if role is OrderAuditRole.STOP_LOSS
                        else DemoTradeCloseReason.STOP_LOSS
                    )
                    if trade.protective_exit_reason != opposite_reason:
                        raise OrderAuditVerificationError(
                            "ORDER_AUDIT_TERMINAL_QUANTITY_INVALID",
                            f"FINISHED protective sibling {role} has zero executed quantity "
                            "but opposite protective exit is not verified",
                        )
            else:
                if actual_order_id is None:
                    raise OrderAuditVerificationError(
                        "ORDER_AUDIT_ACTUAL_ORDER_ID_MISSING",
                        f"FINISHED protective order {role} has executed quantity "
                        "but is missing actualOrderId",
                    )
                if average is None:
                    raise OrderAuditVerificationError(
                        "ORDER_AUDIT_AVERAGE_PRICE_MISSING",
                        f"FINISHED protective order {role} has executed quantity "
                        "but is missing average price",
                    )
                if trade is not None:
                    expected_reason = (
                        DemoTradeCloseReason.STOP_LOSS
                        if role is OrderAuditRole.STOP_LOSS
                        else DemoTradeCloseReason.TAKE_PROFIT
                    )
                    if trade.protective_exit_reason != expected_reason:
                        raise OrderAuditVerificationError(
                            "ORDER_AUDIT_TERMINAL_QUANTITY_INVALID",
                            f"FINISHED protective order {role} has non-zero executed quantity "
                            "but is not marked as the protective exit",
                        )
                    if executed != trade.protective_exit_filled_quantity:
                        raise OrderAuditVerificationError(
                            "ORDER_AUDIT_TERMINAL_QUANTITY_INVALID",
                            f"FINISHED protective order {role} executed quantity {executed} "
                            "does not match trade protective exit filled quantity "
                            f"{trade.protective_exit_filled_quantity}",
                        )
        if (
            status == "FINISHED"
            and role not in _PROTECTIVE_ROLES
            and executed != requested
        ):
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_TERMINAL_QUANTITY_INVALID",
                "Non-protective FINISHED order does not equal requested quantity",
            )
        if status == "REJECTED" and executed != 0:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_REJECTED_STATUS_HAS_FILL",
                "REJECTED order has executed quantity",
            )

    @staticmethod
    def _status_transition_allowed(current: str, incoming: str) -> bool:
        """Treat Binance's CANCELED/CANCELLED spellings as one terminal state."""

        normalized_current = "CANCELED" if current == "CANCELLED" else current
        normalized_incoming = "CANCELED" if incoming == "CANCELLED" else incoming
        if normalized_current == normalized_incoming:
            return True
        if normalized_current in _TERMINAL_STATUSES:
            return False
        if normalized_current == "NEW":
            return normalized_incoming in _ALLOWED_STATUSES - {"NEW"}
        if normalized_current == "PARTIALLY_FILLED":
            return normalized_incoming in _TERMINAL_STATUSES
        return False
