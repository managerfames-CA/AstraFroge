"""Focused BE-03/BE-05 continuous order reconciliation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.schemas.execution import (
    DemoProtectionState,
    DemoTradeLifecycle,
    DemoTradeRecord,
    DemoTradeRecordList,
)
from app.schemas.order_reconciliation import OrderReconciliationState
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.services.order_reconciliation import ContinuousOrderReconciliationService
from app.services.recovery import AutomationRecoveryGate

NOW = datetime(2026, 7, 19, 13, 11, tzinfo=UTC)


def _trade() -> DemoTradeRecord:
    return DemoTradeRecord(
        trade_id="trade-1",
        signal_id="signal-1",
        symbol="BTCUSDT",
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        lifecycle=DemoTradeLifecycle.OPEN,
        protection_state=DemoProtectionState.PROTECTED,
        grade=ScannerGrade.A_PLUS,
        entry_price=Decimal("65000"),
        stop_loss_price=Decimal("64000"),
        take_profit_price=Decimal("67000"),
        exchange_order_id="101",
        client_order_id="entry-1",
        stop_order_id="102",
        stop_client_order_id="stop-1",
        take_profit_order_id="103",
        take_profit_client_order_id="tp-1",
        requested_quantity=Decimal("0.01"),
        executed_quantity=Decimal("0.01"),
        order_status="FILLED",
        tracked_margin_usdt=Decimal("65"),
        opened_at=NOW,
        updated_at=NOW,
    )


class _Trades:
    def trades(self) -> DemoTradeRecordList:
        return DemoTradeRecordList(count=1, trades=[_trade()])


class _Client:
    def __init__(
        self,
        *,
        missing_stop: bool = False,
        unexpected_regular_order: bool = False,
        entry_status: str = "FILLED",
        entry_executed_quantity: str = "0.01",
        protective_status: str = "NEW",
    ) -> None:
        self.missing_stop = missing_stop
        self.unexpected_regular_order = unexpected_regular_order
        self.entry_status = entry_status
        self.entry_executed_quantity = entry_executed_quantity
        self.protective_status = protective_status

    def open_orders(self) -> list[dict[str, object]]:
        if self.unexpected_regular_order:
            return [{"symbol": "ETHUSDT", "clientOrderId": "manual-1"}]
        return []

    def open_algo_orders(self) -> list[dict[str, object]]:
        rows = [
            {
                "symbol": "BTCUSDT",
                "clientOrderId": "tp-1",
                "orderId": "103",
                "status": self.protective_status,
            }
        ]
        if not self.missing_stop:
            rows.append(
                {
                    "symbol": "BTCUSDT",
                    "clientOrderId": "stop-1",
                    "orderId": "102",
                    "status": self.protective_status,
                }
            )
        return rows

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, object]:
        return {
            "symbol": symbol,
            "clientOrderId": orig_client_order_id,
            "orderId": "101",
            "status": self.entry_status,
            "executedQty": self.entry_executed_quantity,
        }

    def query_algo_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, object]:
        order_id = "102" if orig_client_order_id == "stop-1" else "103"
        return {
            "symbol": symbol,
            "clientOrderId": orig_client_order_id,
            "orderId": order_id,
            "status": self.protective_status,
        }


def _ready_gate() -> AutomationRecoveryGate:
    gate = AutomationRecoveryGate()
    gate.begin()
    gate.mark_exchange_reconciled()
    gate.mark_signals_revalidated()
    gate.mark_ready()
    return gate


def test_order_reconciliation_rejects_non_positive_interval() -> None:
    with pytest.raises(ValueError, match="interval must be positive"):
        ContinuousOrderReconciliationService(
            _Trades(), _Client(), _ready_gate(), interval_seconds=0
        )


def test_order_reconciliation_accepts_verified_entry_and_protection() -> None:
    service = ContinuousOrderReconciliationService(
        _Trades(), _Client(), _ready_gate(), now_provider=lambda: NOW
    )

    assert service.latest().state is OrderReconciliationState.NOT_RUN
    report = service.reconcile()

    assert report.state is OrderReconciliationState.IN_SYNC
    assert report.blocking is False
    assert report.findings == []
    assert service.latest() == report


def test_order_reconciliation_detects_missing_protective_order_and_fails_closed() -> None:
    gate = _ready_gate()
    service = ContinuousOrderReconciliationService(
        _Trades(), _Client(missing_stop=True), gate, now_provider=lambda: NOW
    )

    report = service.reconcile()

    assert report.state is OrderReconciliationState.DRIFT_DETECTED
    assert report.blocking is True
    assert "PROTECTIVE_ORDER_MISSING" in {item.code for item in report.findings}
    assert gate.snapshot().automation_ready is False


def test_order_reconciliation_classifies_entry_partial_fill() -> None:
    gate = _ready_gate()
    service = ContinuousOrderReconciliationService(
        _Trades(),
        _Client(
            entry_status="PARTIALLY_FILLED",
            entry_executed_quantity="0.005",
        ),
        gate,
        now_provider=lambda: NOW,
    )

    report = service.reconcile()

    assert "ENTRY_ORDER_PARTIAL_FILL" in {item.code for item in report.findings}
    assert report.blocking is True
    assert gate.snapshot().automation_ready is False


def test_order_reconciliation_classifies_protective_partial_fill() -> None:
    service = ContinuousOrderReconciliationService(
        _Trades(),
        _Client(protective_status="PARTIALLY_FILLED"),
        _ready_gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()

    assert "PROTECTIVE_ORDER_PARTIAL_FILL" in {item.code for item in report.findings}
    assert report.blocking is True


def test_order_reconciliation_fails_closed_without_private_client() -> None:
    gate = _ready_gate()
    service = ContinuousOrderReconciliationService(_Trades(), None, gate, now_provider=lambda: NOW)

    report = service.reconcile()

    assert report.state is OrderReconciliationState.UNAVAILABLE
    assert report.blocking is True
    assert report.findings[0].code == "DEMO_PRIVATE_API_NOT_CONFIGURED"
    assert gate.snapshot().automation_ready is False


def test_order_reconciliation_blocks_unexpected_regular_order() -> None:
    gate = _ready_gate()
    service = ContinuousOrderReconciliationService(
        _Trades(),
        _Client(unexpected_regular_order=True),
        gate,
        now_provider=lambda: NOW,
    )

    report = service.reconcile()

    assert report.state is OrderReconciliationState.DRIFT_DETECTED
    assert "UNEXPECTED_OPEN_REGULAR_ORDER" in {item.code for item in report.findings}
    assert gate.snapshot().automation_ready is False
