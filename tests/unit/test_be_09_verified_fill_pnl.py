"""Focused BE-09 verified-fill realized-PnL tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.schemas.execution import (
    DemoProtectionState,
    DemoTradeCloseReason,
    DemoTradeLifecycle,
    DemoTradeRecord,
)
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.services.journal_exchange_verification import (
    JournalExchangeVerificationService,
    JournalSourceVerificationError,
)

NOW = datetime(2026, 7, 19, 16, 0, tzinfo=UTC)


def _trade(
    *,
    direction: ScannerDirection = ScannerDirection.LONG,
    quantity: Decimal = Decimal("2"),
) -> DemoTradeRecord:
    return DemoTradeRecord(
        trade_id="be09-trade",
        signal_id="be09-signal",
        symbol="BTCUSDT",
        direction=direction,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        lifecycle=DemoTradeLifecycle.CLOSED,
        protection_state=DemoProtectionState.PROTECTED,
        grade=ScannerGrade.A_PLUS,
        entry_price=Decimal("999"),
        stop_loss_price=Decimal("90"),
        take_profit_price=Decimal("120"),
        exit_price=Decimal("998"),
        exchange_order_id="entry-order",
        client_order_id="entry-client",
        stop_order_id="stop-order",
        stop_client_order_id="stop-client",
        take_profit_order_id="take-order",
        take_profit_client_order_id="take-client",
        requested_quantity=quantity,
        executed_quantity=quantity,
        order_status="FILLED",
        tracked_margin_usdt=Decimal("100"),
        realized_pnl_usdt=Decimal("99999"),
        gross_realized_pnl_usdt=Decimal("99998"),
        opened_at=NOW - timedelta(hours=1),
        closed_at=NOW,
        closed_reason=DemoTradeCloseReason.TAKE_PROFIT,
        updated_at=NOW,
    )


class _Client:
    def __init__(self, fills: list[dict[str, Any]]) -> None:
        self._fills = fills

    def query_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]:
        return {
            "clientOrderId": orig_client_order_id,
            "orderId": "entry-order",
            "status": "FILLED",
            "executedQty": "2",
        }

    def query_algo_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]:
        return {
            "clientOrderId": orig_client_order_id,
            "actualOrderId": "close-order",
            "status": "FINISHED",
        }

    def user_trades(
        self,
        *,
        symbol: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return list(self._fills)

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return [
            {
                "symbol": "BTCUSDT",
                "incomeType": "REALIZED_PNL",
                "income": "999999",
                "tranId": "income-1",
            }
        ]


def _fill(
    *,
    fill_id: str,
    order_id: str,
    quantity: str,
    price: str,
) -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "id": fill_id,
        "orderId": order_id,
        "qty": quantity,
        "price": price,
    }


def test_long_multi_fill_pnl_uses_weighted_average_prices() -> None:
    client = _Client(
        [
            _fill(
                fill_id="entry-1",
                order_id="entry-order",
                quantity="1",
                price="100",
            ),
            _fill(
                fill_id="entry-2",
                order_id="entry-order",
                quantity="1",
                price="102",
            ),
            _fill(
                fill_id="close-1",
                order_id="close-order",
                quantity="0.5",
                price="105",
            ),
            _fill(
                fill_id="close-2",
                order_id="close-order",
                quantity="1.5",
                price="107",
            ),
        ]
    )

    evidence = JournalExchangeVerificationService(client).verify(_trade())

    assert evidence.entry_average_price == Decimal("101")
    assert evidence.close_average_price == Decimal("106.5")
    assert evidence.entry_fill_quantity == Decimal("2")
    assert evidence.close_fill_quantity == Decimal("2.0")
    assert evidence.gross_realized_pnl_usdt == Decimal("11.0")


def test_short_pnl_reverses_price_delta() -> None:
    client = _Client(
        [
            _fill(
                fill_id="entry-1",
                order_id="entry-order",
                quantity="2",
                price="200",
            ),
            _fill(
                fill_id="close-1",
                order_id="close-order",
                quantity="2",
                price="190",
            ),
        ]
    )

    evidence = JournalExchangeVerificationService(client).verify(
        _trade(direction=ScannerDirection.SHORT)
    )

    assert evidence.entry_average_price == Decimal("200")
    assert evidence.close_average_price == Decimal("190")
    assert evidence.gross_realized_pnl_usdt == Decimal("20")


def test_fill_quantity_mismatch_fails_closed() -> None:
    client = _Client(
        [
            _fill(
                fill_id="entry-1",
                order_id="entry-order",
                quantity="2",
                price="100",
            ),
            _fill(
                fill_id="close-1",
                order_id="close-order",
                quantity="1",
                price="110",
            ),
        ]
    )

    with pytest.raises(
        JournalSourceVerificationError,
        match="JOURNAL_CLOSE_FILL_QUANTITY_MISMATCH",
    ):
        JournalExchangeVerificationService(client).verify(_trade())


def test_non_positive_fill_price_fails_closed() -> None:
    client = _Client(
        [
            _fill(
                fill_id="entry-1",
                order_id="entry-order",
                quantity="2",
                price="0",
            ),
            _fill(
                fill_id="close-1",
                order_id="close-order",
                quantity="2",
                price="110",
            ),
        ]
    )

    with pytest.raises(
        JournalSourceVerificationError,
        match="JOURNAL_EXCHANGE_DECIMAL_INVALID",
    ):
        JournalExchangeVerificationService(client).verify(_trade())
