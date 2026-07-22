"""Focused BE-06 restart/deployment recovery ownership tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.schemas.execution import (
    DemoProtectionState,
    DemoTradeLifecycle,
    DemoTradeRecord,
    DemoTradeRecordList,
)
from app.schemas.restart_recovery import RestartRecoveryState
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.services.recovery import AutomationRecoveryGate
from app.services.restart_recovery import RestartRecoveryOwnershipService

NOW = datetime(2026, 7, 19, 14, 10, tzinfo=UTC)


def _trade(
    *,
    trade_id: str = "trade-restart-1",
    symbol: str = "BTCUSDT",
    direction: ScannerDirection = ScannerDirection.LONG,
    quantity: str = "0.01",
    stop_client_order_id: str | None = None,
    take_profit_client_order_id: str | None = None,
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
        stop_client_order_id=stop_client_order_id or f"stop-client-{trade_id}",
        take_profit_order_id=f"tp-{trade_id}",
        take_profit_client_order_id=(
            take_profit_client_order_id or f"tp-client-{trade_id}"
        ),
        requested_quantity=Decimal(quantity),
        executed_quantity=Decimal(quantity),
        order_status="FILLED",
        tracked_margin_usdt=Decimal("65"),
        opened_at=NOW,
        updated_at=NOW,
    )


class _Trades:
    def __init__(self, trades: list[DemoTradeRecord] | None = None) -> None:
        self._trades = trades or []

    def trades(self) -> DemoTradeRecordList:
        return DemoTradeRecordList(count=len(self._trades), trades=self._trades)


class _Client:
    def __init__(
        self,
        *,
        orders: list[dict[str, Any]] | None = None,
        positions: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._orders = orders or []
        self._positions = positions or []
        self._error = error

    def open_algo_orders(self) -> list[dict[str, Any]]:
        if self._error is not None:
            raise self._error
        return list(self._orders)

    def positions(self) -> list[dict[str, Any]]:
        if self._error is not None:
            raise self._error
        return list(self._positions)


def _ready_gate() -> AutomationRecoveryGate:
    gate = AutomationRecoveryGate()
    gate.begin()
    gate.mark_exchange_reconciled()
    gate.mark_signals_revalidated()
    gate.mark_ready()
    return gate


def _matching_orders(trade: DemoTradeRecord) -> list[dict[str, Any]]:
    return [
        {
            "symbol": trade.symbol,
            "clientOrderId": trade.stop_client_order_id,
            "orderId": trade.stop_order_id,
            "status": "NEW",
        },
        {
            "symbol": trade.symbol,
            "clientOrderId": trade.take_profit_client_order_id,
            "orderId": trade.take_profit_order_id,
            "status": "NEW",
        },
    ]


def _matching_position(trade: DemoTradeRecord) -> dict[str, Any]:
    signed = trade.executed_quantity
    if trade.direction is ScannerDirection.SHORT:
        signed = -signed
    return {"symbol": trade.symbol, "positionAmt": str(signed)}


def test_restart_recovery_reports_rehydrated_trade_orders_and_position() -> None:
    trade = _trade()
    service = RestartRecoveryOwnershipService(
        _Trades([trade]),
        _Client(
            orders=_matching_orders(trade),
            positions=[_matching_position(trade)],
        ),
        _ready_gate(),
        now_provider=lambda: NOW,
    )

    report = service.report()

    assert report.state is RestartRecoveryState.RECOVERED
    assert report.blocking is False
    assert report.recovered_open_trade_count == 1
    assert report.recovered_open_order_count == 2
    assert report.recovered_open_position_count == 1
    assert report.recovered_trade_ids == [trade.trade_id]
    assert report.recovered_position_symbols == [trade.symbol]
    assert report.recovered_order_client_ids == sorted(
        [trade.stop_client_order_id, trade.take_profit_client_order_id]
    )


def test_restart_recovery_accepts_verified_empty_state() -> None:
    report = RestartRecoveryOwnershipService(
        _Trades(),
        _Client(),
        _ready_gate(),
        now_provider=lambda: NOW,
    ).report()

    assert report.state is RestartRecoveryState.RECOVERED
    assert report.recovered_open_trade_count == 0
    assert report.recovered_open_order_count == 0
    assert report.recovered_open_position_count == 0


def test_restart_recovery_is_not_ready_before_startup_exchange_reconciliation() -> None:
    gate = AutomationRecoveryGate()
    report = RestartRecoveryOwnershipService(
        _Trades([_trade()]),
        _Client(),
        gate,
        now_provider=lambda: NOW,
    ).report()

    assert report.state is RestartRecoveryState.NOT_READY
    assert report.blocking is True
    assert report.error == "STARTUP_EXCHANGE_RECONCILIATION_NOT_COMPLETE"
    assert report.recovered_open_trade_count == 1


def test_restart_recovery_blocks_without_private_client() -> None:
    report = RestartRecoveryOwnershipService(
        _Trades(),
        None,
        _ready_gate(),
        now_provider=lambda: NOW,
    ).report()

    assert report.state is RestartRecoveryState.BLOCKED
    assert report.error == "DEMO_PRIVATE_API_NOT_CONFIGURED"


def test_restart_recovery_blocks_duplicate_durable_position() -> None:
    trades = [_trade(), _trade(trade_id="trade-2")]
    report = RestartRecoveryOwnershipService(
        _Trades(trades),
        _Client(),
        _ready_gate(),
        now_provider=lambda: NOW,
    ).report()

    assert report.error == "DUPLICATE_DURABLE_OPEN_POSITION"


def test_restart_recovery_blocks_duplicate_durable_order_identity() -> None:
    first = _trade()
    second = _trade(
        trade_id="trade-2",
        symbol="ETHUSDT",
        stop_client_order_id=first.stop_client_order_id,
    )
    report = RestartRecoveryOwnershipService(
        _Trades([first, second]),
        _Client(),
        _ready_gate(),
        now_provider=lambda: NOW,
    ).report()

    assert report.error == "DUPLICATE_DURABLE_OPEN_ORDER"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            BinanceDemoPrivateClientError("unavailable"),
            "RESTART_RECOVERY_EXCHANGE_UNAVAILABLE",
        ),
        (RuntimeError("unexpected"), "RESTART_RECOVERY_INVALID"),
    ],
)
def test_restart_recovery_normalizes_exchange_failures(
    error: Exception,
    expected: str,
) -> None:
    report = RestartRecoveryOwnershipService(
        _Trades(),
        _Client(error=error),
        _ready_gate(),
        now_provider=lambda: NOW,
    ).report()

    assert report.state is RestartRecoveryState.BLOCKED
    assert report.error == expected


@pytest.mark.parametrize(
    ("orders", "expected"),
    [
        (
            [{"symbol": "BTCUSDT", "orderId": "1", "status": "NEW"}],
            "RECOVERED_OPEN_ORDER_PAYLOAD_INVALID",
        ),
        (
            [
                {
                    "symbol": "BTCUSDT",
                    "clientOrderId": "stop",
                    "orderId": "1",
                    "status": "PARTIALLY_FILLED",
                }
            ],
            "RECOVERED_OPEN_ORDER_STATUS_UNSAFE",
        ),
        (
            [
                {
                    "symbol": "BTCUSDT",
                    "clientOrderId": "stop",
                    "orderId": "1",
                    "status": "NEW",
                },
                {
                    "symbol": "BTCUSDT",
                    "clientOrderId": "stop",
                    "orderId": "1",
                    "status": "NEW",
                },
            ],
            "DUPLICATE_RECOVERED_OPEN_ORDER",
        ),
    ],
)
def test_restart_recovery_rejects_unsafe_exchange_orders(
    orders: list[dict[str, Any]],
    expected: str,
) -> None:
    report = RestartRecoveryOwnershipService(
        _Trades(),
        _Client(orders=orders),
        _ready_gate(),
        now_provider=lambda: NOW,
    ).report()

    assert report.error == expected


@pytest.mark.parametrize(
    ("positions", "expected"),
    [
        (
            [{"symbol": "", "positionAmt": "1"}],
            "RECOVERED_POSITION_PAYLOAD_INVALID",
        ),
        (
            [{"symbol": "BTCUSDT", "positionAmt": "invalid"}],
            "RECOVERED_POSITION_PAYLOAD_INVALID",
        ),
        (
            [{"symbol": "BTCUSDT", "positionAmt": "NaN"}],
            "RECOVERED_POSITION_PAYLOAD_INVALID",
        ),
        (
            [
                {"symbol": "BTCUSDT", "positionAmt": "1"},
                {"symbol": "BTCUSDT", "positionAmt": "2"},
            ],
            "DUPLICATE_RECOVERED_OPEN_POSITION",
        ),
    ],
)
def test_restart_recovery_rejects_invalid_exchange_positions(
    positions: list[dict[str, Any]],
    expected: str,
) -> None:
    report = RestartRecoveryOwnershipService(
        _Trades(),
        _Client(positions=positions),
        _ready_gate(),
        now_provider=lambda: NOW,
    ).report()

    assert report.error == expected


def test_restart_recovery_blocks_order_set_mismatch_with_observed_ids() -> None:
    trade = _trade()
    report = RestartRecoveryOwnershipService(
        _Trades([trade]),
        _Client(
            orders=[
                {
                    "symbol": trade.symbol,
                    "clientOrderId": trade.stop_client_order_id,
                    "orderId": trade.stop_order_id,
                    "status": "NEW",
                }
            ],
            positions=[_matching_position(trade)],
        ),
        _ready_gate(),
        now_provider=lambda: NOW,
    ).report()

    assert report.error == "RECOVERED_OPEN_ORDER_SET_MISMATCH"
    assert report.recovered_open_order_count == 1
    assert report.recovered_open_position_count == 1


def test_restart_recovery_blocks_position_set_mismatch_with_observed_symbols() -> None:
    trade = _trade(direction=ScannerDirection.SHORT)
    report = RestartRecoveryOwnershipService(
        _Trades([trade]),
        _Client(
            orders=_matching_orders(trade),
            positions=[{"symbol": trade.symbol, "positionAmt": "0.02"}],
        ),
        _ready_gate(),
        now_provider=lambda: NOW,
    ).report()

    assert report.error == "RECOVERED_OPEN_POSITION_SET_MISMATCH"
    assert report.recovered_open_order_count == 2
    assert report.recovered_position_symbols == [trade.symbol]
