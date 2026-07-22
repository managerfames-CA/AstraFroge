"""Focused BE-04/BE-05 continuous position reconciliation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.schemas.execution import (
    DemoProtectionState,
    DemoTradeLifecycle,
    DemoTradeRecord,
    DemoTradeRecordList,
)
from app.schemas.position_reconciliation import PositionReconciliationState
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.services.position_reconciliation import (
    ContinuousPositionReconciliationService,
)
from app.services.recovery import AutomationRecoveryGate

NOW = datetime(2026, 7, 19, 13, 32, tzinfo=UTC)


def _trade(
    *,
    trade_id: str = "trade-1",
    symbol: str = "BTCUSDT",
    direction: ScannerDirection = ScannerDirection.LONG,
    quantity: str = "0.01",
) -> DemoTradeRecord:
    return DemoTradeRecord(
        trade_id=trade_id,
        signal_id=f"signal-{trade_id}",
        symbol=symbol,
        direction=direction,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        lifecycle=DemoTradeLifecycle.OPEN,
        protection_state=DemoProtectionState.PROTECTED,
        grade=ScannerGrade.A_PLUS,
        entry_price=Decimal("65000"),
        stop_loss_price=Decimal("64000"),
        take_profit_price=Decimal("67000"),
        exchange_order_id=f"entry-{trade_id}",
        client_order_id=f"entry-client-{trade_id}",
        stop_order_id=f"stop-{trade_id}",
        stop_client_order_id=f"stop-client-{trade_id}",
        take_profit_order_id=f"tp-{trade_id}",
        take_profit_client_order_id=f"tp-client-{trade_id}",
        requested_quantity=Decimal(quantity),
        executed_quantity=Decimal(quantity),
        order_status="FILLED",
        tracked_margin_usdt=Decimal("65"),
        opened_at=NOW,
        updated_at=NOW,
    )


class _Trades:
    def __init__(self, trades: list[DemoTradeRecord] | None = None) -> None:
        self._trades = trades or [_trade()]

    def trades(self) -> DemoTradeRecordList:
        return DemoTradeRecordList(count=len(self._trades), trades=self._trades)


class _Client:
    def __init__(
        self,
        positions: list[dict[str, object]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._positions = positions or [
            {"symbol": "BTCUSDT", "positionAmt": "0.01"}
        ]
        self._error = error

    def positions(self) -> list[dict[str, object]]:
        if self._error is not None:
            raise self._error
        return self._positions


def _ready_gate() -> AutomationRecoveryGate:
    gate = AutomationRecoveryGate()
    gate.begin()
    gate.mark_exchange_reconciled()
    gate.mark_signals_revalidated()
    gate.mark_ready()
    return gate


def test_position_reconciliation_accepts_matching_exchange_position() -> None:
    service = ContinuousPositionReconciliationService(
        _Trades(),
        _Client(),
        _ready_gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()

    assert report.state is PositionReconciliationState.IN_SYNC
    assert report.blocking is False
    assert report.exchange_open_position_count == 1
    assert report.findings == []
    assert service.latest() == report


def test_position_reconciliation_detects_external_close_and_fails_closed() -> None:
    gate = _ready_gate()
    service = ContinuousPositionReconciliationService(
        _Trades(),
        _Client([{"symbol": "BTCUSDT", "positionAmt": "0"}]),
        gate,
        now_provider=lambda: NOW,
    )

    report = service.reconcile()

    assert report.state is PositionReconciliationState.DRIFT_DETECTED
    assert {item.code for item in report.findings} == {
        "EXTERNAL_POSITION_CLOSE_DETECTED"
    }
    assert gate.snapshot().automation_ready is False


def test_position_reconciliation_detects_direction_and_quantity_mismatch() -> None:
    service = ContinuousPositionReconciliationService(
        _Trades(),
        _Client([{"symbol": "BTCUSDT", "positionAmt": "-0.02"}]),
        _ready_gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()
    codes = {item.code for item in report.findings}

    assert "EXCHANGE_POSITION_DIRECTION_MISMATCH" in codes
    assert "EXCHANGE_POSITION_QUANTITY_MISMATCH" in codes
    assert report.blocking is True


def test_position_reconciliation_detects_orphan_duplicate_and_invalid_positions() -> None:
    positions: list[dict[str, object]] = [
        {"symbol": "BTCUSDT", "positionAmt": "0.01"},
        {"symbol": "BTCUSDT", "positionAmt": "0.02"},
        {"symbol": "ETHUSDT", "positionAmt": "1"},
        {"symbol": "", "positionAmt": "1"},
        {"symbol": "BNBUSDT", "positionAmt": "not-a-number"},
    ]
    service = ContinuousPositionReconciliationService(
        _Trades(),
        _Client(positions),
        _ready_gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()
    codes = {item.code for item in report.findings}

    assert "DUPLICATE_EXCHANGE_OPEN_POSITION" in codes
    assert "ORPHAN_EXCHANGE_POSITION" in codes
    assert "EXCHANGE_POSITION_PAYLOAD_INVALID" in codes


def test_position_reconciliation_detects_duplicate_local_open_trade() -> None:
    service = ContinuousPositionReconciliationService(
        _Trades([_trade(), _trade(trade_id="trade-2")]),
        _Client(),
        _ready_gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()

    assert "DUPLICATE_LOCAL_OPEN_POSITION" in {
        item.code for item in report.findings
    }


def test_position_reconciliation_fails_closed_without_private_client() -> None:
    gate = _ready_gate()
    service = ContinuousPositionReconciliationService(
        _Trades(),
        None,
        gate,
        now_provider=lambda: NOW,
    )

    report = service.reconcile()

    assert report.state is PositionReconciliationState.UNAVAILABLE
    assert report.findings[0].code == "DEMO_PRIVATE_API_NOT_CONFIGURED"
    assert gate.snapshot().automation_ready is False


def test_position_reconciliation_normalizes_exchange_and_unknown_failures() -> None:
    exchange_service = ContinuousPositionReconciliationService(
        _Trades(),
        _Client(error=BinanceDemoPrivateClientError("unavailable")),
        _ready_gate(),
        now_provider=lambda: NOW,
    )
    unknown_service = ContinuousPositionReconciliationService(
        _Trades(),
        _Client(error=RuntimeError("unexpected")),
        _ready_gate(),
        now_provider=lambda: NOW,
    )

    exchange_report = exchange_service.reconcile()
    unknown_report = unknown_service.reconcile()

    assert exchange_report.findings[0].code == "POSITION_RECONCILIATION_UNAVAILABLE"
    assert unknown_report.findings[0].code == "POSITION_RECONCILIATION_INVALID"


def test_position_reconciliation_rejects_non_positive_interval() -> None:
    with pytest.raises(ValueError, match="interval must be positive"):
        ContinuousPositionReconciliationService(
            _Trades(),
            _Client(),
            _ready_gate(),
            interval_seconds=0,
        )
