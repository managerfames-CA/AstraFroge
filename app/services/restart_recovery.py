"""Read-only proof of restart/deployment recovery ownership."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.schemas.execution import DemoTradeLifecycle, DemoTradeRecordList
from app.schemas.restart_recovery import RestartRecoveryReport, RestartRecoveryState
from app.schemas.scanner import ScannerDirection
from app.services.recovery import AutomationRecoveryGate


class RestartRecoveryClient(Protocol):
    """Read-only Binance Demo surface required for ownership proof."""

    def open_algo_orders(self) -> list[dict[str, Any]]: ...

    def positions(self) -> list[dict[str, Any]]: ...


class RestartRecoveryTradeSource(Protocol):
    """Durable execution surface rehydrated during process construction."""

    def trades(self) -> DemoTradeRecordList: ...


class RestartRecoveryOwnershipService:
    """Prove that rehydrated durable trades own current Demo orders/positions."""

    def __init__(
        self,
        trade_source: RestartRecoveryTradeSource,
        client: RestartRecoveryClient | None,
        recovery_gate: AutomationRecoveryGate,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._trade_source = trade_source
        self._client = client
        self._gate = recovery_gate
        self._now = now_provider or (lambda: datetime.now(UTC))

    def report(self) -> RestartRecoveryReport:
        """Return one secret-safe recovered-ownership report without mutations."""

        checked_at = self._now()
        recovery = self._gate.snapshot()
        trades = [
            trade
            for trade in self._trade_source.trades().trades
            if trade.lifecycle is DemoTradeLifecycle.OPEN
        ]
        trade_ids = sorted(trade.trade_id for trade in trades)

        if not recovery.exchange_reconciled:
            return self._blocked(
                checked_at,
                trade_ids,
                recovery.recovery_state,
                recovery.automation_ready,
                "STARTUP_EXCHANGE_RECONCILIATION_NOT_COMPLETE",
                state=RestartRecoveryState.NOT_READY,
            )

        client = self._client
        if client is None:
            return self._blocked(
                checked_at,
                trade_ids,
                recovery.recovery_state,
                recovery.automation_ready,
                "DEMO_PRIVATE_API_NOT_CONFIGURED",
            )

        expected_orders: dict[str, tuple[str, str]] = {}
        expected_positions: dict[str, tuple[ScannerDirection, Decimal]] = {}
        for trade in trades:
            if trade.symbol in expected_positions:
                return self._blocked(
                    checked_at,
                    trade_ids,
                    recovery.recovery_state,
                    recovery.automation_ready,
                    "DUPLICATE_DURABLE_OPEN_POSITION",
                )
            expected_positions[trade.symbol] = (trade.direction, trade.executed_quantity)
            for client_order_id, exchange_order_id in (
                (trade.stop_client_order_id, trade.stop_order_id),
                (trade.take_profit_client_order_id, trade.take_profit_order_id),
            ):
                if client_order_id in expected_orders:
                    return self._blocked(
                        checked_at,
                        trade_ids,
                        recovery.recovery_state,
                        recovery.automation_ready,
                        "DUPLICATE_DURABLE_OPEN_ORDER",
                    )
                expected_orders[client_order_id] = (trade.symbol, exchange_order_id)

        try:
            actual_orders = self._open_orders(client.open_algo_orders())
            actual_positions = self._open_positions(client.positions())
        except BinanceDemoPrivateClientError:
            return self._blocked(
                checked_at,
                trade_ids,
                recovery.recovery_state,
                recovery.automation_ready,
                "RESTART_RECOVERY_EXCHANGE_UNAVAILABLE",
            )
        except ValueError as exc:
            return self._blocked(
                checked_at,
                trade_ids,
                recovery.recovery_state,
                recovery.automation_ready,
                str(exc),
            )
        except Exception:
            return self._blocked(
                checked_at,
                trade_ids,
                recovery.recovery_state,
                recovery.automation_ready,
                "RESTART_RECOVERY_INVALID",
            )

        if actual_orders != expected_orders:
            return self._blocked(
                checked_at,
                trade_ids,
                recovery.recovery_state,
                recovery.automation_ready,
                "RECOVERED_OPEN_ORDER_SET_MISMATCH",
                order_client_ids=sorted(actual_orders),
                position_symbols=sorted(actual_positions),
            )
        if actual_positions != expected_positions:
            return self._blocked(
                checked_at,
                trade_ids,
                recovery.recovery_state,
                recovery.automation_ready,
                "RECOVERED_OPEN_POSITION_SET_MISMATCH",
                order_client_ids=sorted(actual_orders),
                position_symbols=sorted(actual_positions),
            )

        return RestartRecoveryReport(
            state=RestartRecoveryState.RECOVERED,
            checked_at=checked_at,
            recovery_state=recovery.recovery_state,
            exchange_reconciled=True,
            automation_ready=recovery.automation_ready,
            recovered_open_trade_count=len(trades),
            recovered_open_order_count=len(actual_orders),
            recovered_open_position_count=len(actual_positions),
            recovered_trade_ids=trade_ids,
            recovered_order_client_ids=sorted(actual_orders),
            recovered_position_symbols=sorted(actual_positions),
            blocking=False,
            error=None,
        )

    @classmethod
    def _open_orders(
        cls,
        payloads: list[dict[str, Any]],
    ) -> dict[str, tuple[str, str]]:
        orders: dict[str, tuple[str, str]] = {}
        for payload in payloads:
            client_order_id = cls._text(payload.get("clientOrderId"))
            exchange_order_id = cls._text(payload.get("orderId"))
            symbol = cls._text(payload.get("symbol"))
            status = cls._text(payload.get("status"))
            if client_order_id is None or exchange_order_id is None or symbol is None:
                raise ValueError("RECOVERED_OPEN_ORDER_PAYLOAD_INVALID")
            if status != "NEW":
                raise ValueError("RECOVERED_OPEN_ORDER_STATUS_UNSAFE")
            if client_order_id in orders:
                raise ValueError("DUPLICATE_RECOVERED_OPEN_ORDER")
            orders[client_order_id] = (symbol, exchange_order_id)
        return orders

    @classmethod
    def _open_positions(
        cls,
        payloads: list[dict[str, Any]],
    ) -> dict[str, tuple[ScannerDirection, Decimal]]:
        positions: dict[str, tuple[ScannerDirection, Decimal]] = {}
        for payload in payloads:
            symbol = cls._text(payload.get("symbol"))
            if symbol is None:
                raise ValueError("RECOVERED_POSITION_PAYLOAD_INVALID")
            quantity = cls._decimal(payload.get("positionAmt"))
            if quantity == 0:
                continue
            if symbol in positions:
                raise ValueError("DUPLICATE_RECOVERED_OPEN_POSITION")
            positions[symbol] = (
                ScannerDirection.LONG if quantity > 0 else ScannerDirection.SHORT,
                abs(quantity),
            )
        return positions

    def _blocked(
        self,
        checked_at: datetime,
        trade_ids: list[str],
        recovery_state: Any,
        automation_ready: bool,
        error: str,
        *,
        state: RestartRecoveryState = RestartRecoveryState.BLOCKED,
        order_client_ids: list[str] | None = None,
        position_symbols: list[str] | None = None,
    ) -> RestartRecoveryReport:
        return RestartRecoveryReport(
            state=state,
            checked_at=checked_at,
            recovery_state=recovery_state,
            exchange_reconciled=self._gate.snapshot().exchange_reconciled,
            automation_ready=automation_ready,
            recovered_open_trade_count=len(trade_ids),
            recovered_open_order_count=len(order_client_ids or []),
            recovered_open_position_count=len(position_symbols or []),
            recovered_trade_ids=trade_ids,
            recovered_order_client_ids=order_client_ids or [],
            recovered_position_symbols=position_symbols or [],
            blocking=True,
            error=error,
        )

    @staticmethod
    def _text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("RECOVERED_POSITION_PAYLOAD_INVALID") from exc
        if not parsed.is_finite():
            raise ValueError("RECOVERED_POSITION_PAYLOAD_INVALID")
        return parsed
