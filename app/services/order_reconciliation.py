"""Continuous read-only reconciliation of Binance Demo orders."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Any, Protocol

from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.schemas.execution import DemoTradeLifecycle, DemoTradeRecord
from app.schemas.order_reconciliation import (
    OrderReconciliationFinding,
    OrderReconciliationReport,
    OrderReconciliationState,
)
from app.services.recovery import AutomationRecoveryGate

_INDEXABLE_ALGO_STATUSES = frozenset({"NEW", "PARTIALLY_FILLED"})
_SAFE_OPEN_ALGO_STATUSES = frozenset({"NEW"})


class OrderReconciliationClient(Protocol):
    def open_orders(self) -> list[dict[str, Any]]: ...

    def open_algo_orders(self) -> list[dict[str, Any]]: ...

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]: ...

    def query_algo_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]: ...


class TradeList(Protocol):
    trades: list[DemoTradeRecord]


class TradeSource(Protocol):
    def trades(self) -> TradeList: ...


class ContinuousOrderReconciliationService:
    """Continuously compare durable order identities with Binance Demo truth."""

    def __init__(
        self,
        trade_source: TradeSource,
        client: OrderReconciliationClient | None,
        recovery_gate: AutomationRecoveryGate,
        *,
        interval_seconds: float = 15.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Reconciliation interval must be positive")
        self._trade_source = trade_source
        self._client = client
        self._gate = recovery_gate
        self._interval_seconds = interval_seconds
        self._now = now_provider or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._latest = OrderReconciliationReport(
            state=OrderReconciliationState.NOT_RUN,
            checked_at=self._now(),
            local_open_trade_count=0,
            exchange_open_regular_order_count=0,
            exchange_open_algo_order_count=0,
            blocking=False,
            findings=[],
        )

    def latest(self) -> OrderReconciliationReport:
        with self._lock:
            return self._latest.model_copy(deep=True)

    async def run_forever(self) -> None:
        while True:
            if self._gate.snapshot().automation_ready:
                report = await asyncio.to_thread(self.reconcile)
                if report.blocking:
                    return
            await asyncio.sleep(self._interval_seconds)

    def reconcile(self) -> OrderReconciliationReport:
        checked_at = self._now()
        trades = [
            trade
            for trade in self._trade_source.trades().trades
            if trade.lifecycle is DemoTradeLifecycle.OPEN
        ]
        client = self._client
        if client is None:
            return self._publish_unavailable(
                checked_at,
                len(trades),
                "DEMO_PRIVATE_API_NOT_CONFIGURED",
                "Binance Demo private API is unavailable for order reconciliation",
            )

        try:
            regular_orders = client.open_orders()
            algo_orders = client.open_algo_orders()
            findings = self._compare(trades, regular_orders, algo_orders, client)
        except BinanceDemoPrivateClientError:
            return self._publish_unavailable(
                checked_at,
                len(trades),
                "ORDER_RECONCILIATION_UNAVAILABLE",
                "Binance Demo order reconciliation request failed",
            )
        except Exception:
            return self._publish_unavailable(
                checked_at,
                len(trades),
                "ORDER_RECONCILIATION_INVALID",
                "Order reconciliation could not prove a safe exchange state",
            )

        blocking = any(item.blocking for item in findings)
        report = OrderReconciliationReport(
            state=(
                OrderReconciliationState.DRIFT_DETECTED
                if blocking
                else OrderReconciliationState.IN_SYNC
            ),
            checked_at=checked_at,
            local_open_trade_count=len(trades),
            exchange_open_regular_order_count=len(regular_orders),
            exchange_open_algo_order_count=len(algo_orders),
            blocking=blocking,
            findings=findings,
        )
        self._publish(report)
        if blocking:
            self._gate.fail("CONTINUOUS_ORDER_RECONCILIATION_DRIFT")
        return report

    def _compare(
        self,
        trades: list[DemoTradeRecord],
        regular_orders: list[dict[str, Any]],
        algo_orders: list[dict[str, Any]],
        client: OrderReconciliationClient,
    ) -> list[OrderReconciliationFinding]:
        findings: list[OrderReconciliationFinding] = []
        expected_algos: dict[str, tuple[str, str, str]] = {}

        for payload in regular_orders:
            findings.append(
                OrderReconciliationFinding(
                    code="UNEXPECTED_OPEN_REGULAR_ORDER",
                    message="An unverified regular order remains open on Binance Demo",
                    symbol=self._text(payload.get("symbol")),
                    client_order_id=self._text(payload.get("clientOrderId")),
                )
            )

        for trade in trades:
            entry = client.query_order(
                symbol=trade.symbol,
                orig_client_order_id=trade.client_order_id,
            )
            try:
                executed_quantity = self._decimal(entry.get("executedQty"))
            except ValueError:
                executed_quantity = Decimal("-1")
            entry_client_id = self._text(entry.get("clientOrderId"))
            entry_order_id = self._text(entry.get("orderId"))
            entry_status = self._text(entry.get("status"))
            if (
                entry_client_id != trade.client_order_id
                or entry_order_id != trade.exchange_order_id
            ):
                findings.append(
                    self._trade_finding(
                        "ENTRY_ORDER_IDENTITY_MISMATCH",
                        "Entry order identity differs from durable state",
                        trade,
                        trade.client_order_id,
                    )
                )
            elif (
                entry_status == "PARTIALLY_FILLED"
                or (executed_quantity > 0 and executed_quantity < trade.requested_quantity)
                or trade.executed_quantity < trade.requested_quantity
            ):
                findings.append(
                    self._trade_finding(
                        "ENTRY_ORDER_PARTIAL_FILL",
                        "Entry order is only partially filled relative to requested quantity",
                        trade,
                        trade.client_order_id,
                    )
                )
            elif entry_status != "FILLED":
                findings.append(
                    self._trade_finding(
                        "ENTRY_ORDER_STATUS_MISMATCH",
                        "Entry order is not exchange-confirmed as filled",
                        trade,
                        trade.client_order_id,
                    )
                )
            elif executed_quantity != trade.executed_quantity:
                findings.append(
                    self._trade_finding(
                        "ENTRY_FILL_QUANTITY_MISMATCH",
                        "Entry fill quantity differs from durable state",
                        trade,
                        trade.client_order_id,
                    )
                )

            for kind, client_order_id, expected_order_id in (
                ("STOP_LOSS", trade.stop_client_order_id, trade.stop_order_id),
                (
                    "TAKE_PROFIT",
                    trade.take_profit_client_order_id,
                    trade.take_profit_order_id,
                ),
            ):
                if client_order_id in expected_algos:
                    findings.append(
                        self._trade_finding(
                            "DUPLICATE_LOCAL_PROTECTIVE_ORDER",
                            "A protective client-order identity is reused",
                            trade,
                            client_order_id,
                        )
                    )
                expected_algos[client_order_id] = (
                    trade.symbol,
                    expected_order_id,
                    kind,
                )

        actual_algos: dict[str, tuple[str, str]] = {}
        for payload in algo_orders:
            client_id = self._text(payload.get("clientOrderId"))
            exchange_order_id = self._text(payload.get("orderId"))
            symbol = self._text(payload.get("symbol"))
            algo_status = self._text(payload.get("status"))
            if client_id is None or exchange_order_id is None or symbol is None:
                findings.append(
                    OrderReconciliationFinding(
                        code="OPEN_ALGO_ORDER_PAYLOAD_INVALID",
                        message="An open protective order has invalid identity fields",
                        symbol=symbol,
                        client_order_id=client_id,
                    )
                )
                continue
            if algo_status not in _INDEXABLE_ALGO_STATUSES:
                findings.append(
                    OrderReconciliationFinding(
                        code="OPEN_ALGO_ORDER_STATUS_INVALID",
                        message="An open protective order has an unsafe status",
                        symbol=symbol,
                        client_order_id=client_id,
                    )
                )
            if client_id in actual_algos:
                findings.append(
                    OrderReconciliationFinding(
                        code="DUPLICATE_EXCHANGE_OPEN_ALGO_ORDER",
                        message="Binance Demo returned a duplicate protective identity",
                        symbol=symbol,
                        client_order_id=client_id,
                    )
                )
            actual_algos[client_id] = (symbol, exchange_order_id)

        for client_id, (symbol, expected_order_id, _kind) in expected_algos.items():
            actual = actual_algos.get(client_id)
            matched_trade = next(
                (item for item in trades if item.symbol == symbol),
                None,
            )
            if actual is None:
                findings.append(
                    OrderReconciliationFinding(
                        code="PROTECTIVE_ORDER_MISSING",
                        message="A durable open trade is missing required exchange protection",
                        symbol=symbol,
                        trade_id=(matched_trade.trade_id if matched_trade is not None else None),
                        client_order_id=client_id,
                    )
                )
                continue
            if actual != (symbol, expected_order_id):
                findings.append(
                    OrderReconciliationFinding(
                        code="PROTECTIVE_ORDER_IDENTITY_MISMATCH",
                        message="Protective order identity differs from durable state",
                        symbol=symbol,
                        trade_id=(matched_trade.trade_id if matched_trade is not None else None),
                        client_order_id=client_id,
                    )
                )
                continue
            queried = client.query_algo_order(
                symbol=symbol,
                orig_client_order_id=client_id,
            )
            queried_client_id = self._text(queried.get("clientOrderId"))
            queried_order_id = self._text(queried.get("orderId"))
            queried_status = self._text(queried.get("status"))
            if queried_client_id != client_id or queried_order_id != expected_order_id:
                findings.append(
                    OrderReconciliationFinding(
                        code="PROTECTIVE_ORDER_RECONCILIATION_MISMATCH",
                        message="Protective order query does not match durable identity",
                        symbol=symbol,
                        trade_id=(matched_trade.trade_id if matched_trade is not None else None),
                        client_order_id=client_id,
                    )
                )
            elif queried_status == "PARTIALLY_FILLED":
                findings.append(
                    OrderReconciliationFinding(
                        code="PROTECTIVE_ORDER_PARTIAL_FILL",
                        message="Protective order has partially filled while trade remains open",
                        symbol=symbol,
                        trade_id=(matched_trade.trade_id if matched_trade is not None else None),
                        client_order_id=client_id,
                    )
                )
            elif queried_status not in _SAFE_OPEN_ALGO_STATUSES:
                findings.append(
                    OrderReconciliationFinding(
                        code="PROTECTIVE_ORDER_RECONCILIATION_MISMATCH",
                        message="Protective order is not safely open on Binance Demo",
                        symbol=symbol,
                        trade_id=(matched_trade.trade_id if matched_trade is not None else None),
                        client_order_id=client_id,
                    )
                )

        for client_id, (symbol, _exchange_order_id) in actual_algos.items():
            if client_id not in expected_algos:
                findings.append(
                    OrderReconciliationFinding(
                        code="ORPHAN_PROTECTIVE_ORDER",
                        message="Binance Demo has protection with no durable tracked trade",
                        symbol=symbol,
                        client_order_id=client_id,
                    )
                )
        return findings

    def _publish_unavailable(
        self,
        checked_at: datetime,
        local_count: int,
        code: str,
        message: str,
    ) -> OrderReconciliationReport:
        report = OrderReconciliationReport(
            state=OrderReconciliationState.UNAVAILABLE,
            checked_at=checked_at,
            local_open_trade_count=local_count,
            exchange_open_regular_order_count=0,
            exchange_open_algo_order_count=0,
            blocking=True,
            findings=[OrderReconciliationFinding(code=code, message=message)],
        )
        self._publish(report)
        self._gate.fail(code)
        return report

    def _publish(self, report: OrderReconciliationReport) -> None:
        with self._lock:
            self._latest = report

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
            raise ValueError("Invalid decimal") from exc
        if not parsed.is_finite():
            raise ValueError("Invalid decimal")
        return parsed

    @staticmethod
    def _trade_finding(
        code: str,
        message: str,
        trade: DemoTradeRecord,
        client_order_id: str,
    ) -> OrderReconciliationFinding:
        return OrderReconciliationFinding(
            code=code,
            message=message,
            symbol=trade.symbol,
            trade_id=trade.trade_id,
            client_order_id=client_order_id,
        )
