"""Exchange-authoritative Trade Management Engine unit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.core.errors import AppError
from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.schemas.execution import (
    DemoExecutionState,
    DemoExecutionStatusResponse,
    DemoExecutionSummary,
    DemoProtectionState,
    DemoTradeLifecycle,
    DemoTradeRecord,
)
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.trade_management import (
    TradeCloseReason,
    TradeCloseRequest,
    TradeListFilters,
    TradeManagementState,
    TradeSortBy,
)
from app.services.trade_management import TradeManagementService

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _trade(
    *,
    trade_id: str,
    signal_id: str,
    symbol: str,
    direction: ScannerDirection,
    grade: ScannerGrade,
    entry_price: Decimal,
    margin: Decimal,
    unrealized: Decimal,
) -> DemoTradeRecord:
    return DemoTradeRecord(
        trade_id=trade_id,
        signal_id=signal_id,
        symbol=symbol,
        direction=direction,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        lifecycle=DemoTradeLifecycle.OPEN,
        protection_state=DemoProtectionState.PROTECTED,
        grade=grade,
        entry_price=entry_price,
        stop_loss_price=Decimal("95") if direction is ScannerDirection.LONG else Decimal("105"),
        take_profit_price=(Decimal("110") if direction is ScannerDirection.LONG else Decimal("90")),
        exchange_order_id=f"entry-{trade_id[:4]}",
        client_order_id=f"af-e-{signal_id[:20]}",
        stop_order_id=f"stop-{trade_id[:4]}",
        stop_client_order_id=f"af-s-{signal_id[:20]}",
        take_profit_order_id=f"take-{trade_id[:4]}",
        take_profit_client_order_id=f"af-t-{signal_id[:20]}",
        requested_quantity=Decimal("0.1"),
        executed_quantity=Decimal("0.1"),
        order_status="FILLED",
        tracked_margin_usdt=margin,
        unrealized_pnl_usdt=unrealized,
        opened_at=NOW,
        updated_at=NOW,
    )


class StubExecution:
    def __init__(self) -> None:
        self._trades = {
            "a" * 36: _trade(
                trade_id="a" * 36,
                signal_id="1" * 64,
                symbol="BTCUSDT",
                direction=ScannerDirection.LONG,
                grade=ScannerGrade.A_PLUS,
                entry_price=Decimal("101"),
                margin=Decimal("25"),
                unrealized=Decimal("3.5"),
            ),
            "b" * 36: _trade(
                trade_id="b" * 36,
                signal_id="2" * 64,
                symbol="ETHUSDT",
                direction=ScannerDirection.SHORT,
                grade=ScannerGrade.A,
                entry_price=Decimal("99"),
                margin=Decimal("15"),
                unrealized=Decimal("-1.5"),
            ),
        }

    def status(self) -> DemoExecutionStatusResponse:
        return DemoExecutionStatusResponse(
            state=DemoExecutionState.READY,
            execution_enabled=True,
            demo_credentials_configured=True,
            private_api_available=True,
            risk_engine_state="READY",
            take_profit_r_multiple=Decimal("2"),
            max_open_trades_limit=4,
            tracked_trade_count=len(self._trades),
            available_tracking_slots=2,
            combined_unrealized_pnl_usdt=Decimal("2"),
            total_tracked_margin_usdt=Decimal("40"),
            updated_at=NOW,
            summary=DemoExecutionSummary(open_trades=2, long_demo=1, short_demo=1),
        )

    def trades(self):  # type: ignore[no-untyped-def]
        class _TradeList:
            def __init__(self, trades) -> None:
                self.trades = list(trades)

        return _TradeList(self._trades.values())

    def get_trade(self, trade_id: str) -> DemoTradeRecord | None:
        return self._trades.get(trade_id)

    def store_trade(self, trade: DemoTradeRecord) -> DemoTradeRecord:
        self._trades[trade.trade_id] = trade
        return trade


class StubCloseClient:
    def __init__(
        self,
        *,
        exit_price: str = "105",
        close_quantity: str = "0.1",
        income_payload: list[dict[str, Any]] | None = None,
        fail_income: bool = False,
    ) -> None:
        self.exit_price = exit_price
        self.close_quantity = close_quantity
        self.income_payload = income_payload or []
        self.fail_income = fail_income
        self.market_orders: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.income_windows: list[tuple[int, int, int]] = []

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        raise BinanceDemoPrivateClientError(
            "Order does not exist",
            exchange_code=-2013,
        )

    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str,
        new_client_order_id: str,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        self.market_orders.append(
            {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "new_client_order_id": new_client_order_id,
                "reduce_only": reduce_only,
            }
        )
        return {
            "orderId": 9001,
            "clientOrderId": new_client_order_id,
            "status": "FILLED",
            "executedQty": self.close_quantity,
            "avgPrice": self.exit_price,
        }

    def cancel_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        self.cancelled.append(orig_client_order_id)
        return {"symbol": symbol, "clientOrderId": orig_client_order_id}

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        self.income_windows.append((start_time_ms, end_time_ms, limit))
        if self.fail_income:
            raise BinanceDemoPrivateClientError("income unavailable")
        return self.income_payload


def test_trade_management_summarizes_open_trades() -> None:
    service = TradeManagementService(StubExecution())  # type: ignore[arg-type]

    status = service.status()
    assert status.state is TradeManagementState.READY
    assert status.summary.manual_demo_trades == 2
    assert status.summary.long_demo == 1
    assert status.summary.short_demo == 1
    assert status.summary.combined_unrealized_pnl_usdt == Decimal("2.0")
    assert status.summary.total_tracked_margin_usdt == Decimal("40")


def test_trade_management_waits_when_execution_is_locked() -> None:
    class LockedExecution(StubExecution):
        def status(self) -> DemoExecutionStatusResponse:
            status = super().status()
            return status.model_copy(update={"state": DemoExecutionState.EXECUTION_LOCKED})

    service = TradeManagementService(LockedExecution())  # type: ignore[arg-type]

    assert service.status().state is TradeManagementState.WAITING_FOR_EXECUTION


def test_trade_management_filters_and_sorts_trades() -> None:
    service = TradeManagementService(StubExecution())  # type: ignore[arg-type]

    trades = service.trades(
        TradeListFilters(
            direction=ScannerDirection.SHORT,
            min_grade=ScannerGrade.A,
            sort_by=TradeSortBy.UNREALIZED_PNL_ASC,
        )
    )

    assert trades.count == 1
    assert trades.trades[0].symbol == "ETHUSDT"


def test_trade_management_closes_long_from_verified_demo_fill() -> None:
    execution = StubExecution()
    client = StubCloseClient(
        exit_price="105",
        income_payload=[
            {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "0.4"},
            {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-0.01"},
            {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-0.01"},
        ],
    )
    service = TradeManagementService(
        execution,  # type: ignore[arg-type]
        client,
        now_provider=lambda: NOW,
    )

    trade = service.close_trade(
        "a" * 36,
        TradeCloseRequest(reason=TradeCloseReason.MANUAL_CLOSE),
    )

    assert trade.lifecycle is DemoTradeLifecycle.CLOSED
    assert trade.exit_price == Decimal("105")
    assert trade.realized_pnl_usdt == Decimal("0.38")
    assert trade.gross_realized_pnl_usdt == Decimal("0.4")
    assert trade.commission_usdt == Decimal("-0.02")
    assert trade.funding_fees_usdt == Decimal("0")
    assert trade.closed_reason is not None
    assert client.market_orders == [
        {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "quantity": "0.1",
            "new_client_order_id": f"af-m-{'1' * 20}",
            "reduce_only": True,
        }
    ]
    assert client.cancelled == [f"af-s-{'1' * 20}", f"af-t-{'1' * 20}"]


def test_trade_management_fails_closed_when_income_is_unavailable() -> None:
    execution = StubExecution()
    service = TradeManagementService(
        execution,  # type: ignore[arg-type]
        StubCloseClient(exit_price="105", fail_income=True),
        now_provider=lambda: NOW,
    )

    with pytest.raises(AppError) as exc:
        service.close_trade(
            "a" * 36,
            TradeCloseRequest(reason=TradeCloseReason.MANUAL_CLOSE),
        )

    assert exc.value.code == "DEMO_INCOME_RECONCILIATION_FAILED"
    assert execution.get_trade("a" * 36).lifecycle is DemoTradeLifecycle.OPEN


def test_trade_management_fails_closed_without_verified_realized_pnl() -> None:
    execution = StubExecution()
    service = TradeManagementService(
        execution,  # type: ignore[arg-type]
        StubCloseClient(
            exit_price="105",
            income_payload=[{"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-0.01"}],
        ),
        now_provider=lambda: NOW,
    )

    with pytest.raises(AppError) as exc:
        service.close_trade("a" * 36, TradeCloseRequest())

    assert exc.value.code == "DEMO_REALIZED_PNL_UNVERIFIED"
    assert execution.get_trade("a" * 36).lifecycle is DemoTradeLifecycle.OPEN


def test_trade_management_rejects_client_exit_and_pnl() -> None:
    service = TradeManagementService(
        StubExecution(),  # type: ignore[arg-type]
        StubCloseClient(),
    )

    with pytest.raises(AppError) as exc:
        service.close_trade(
            "a" * 36,
            TradeCloseRequest(
                exit_price=Decimal("999"),
                realized_pnl_usdt=Decimal("99"),
            ),
        )
    assert exc.value.code == "CLIENT_CLOSE_VALUES_NOT_ALLOWED"


def test_trade_management_rejects_partial_close_fill() -> None:
    service = TradeManagementService(
        StubExecution(),  # type: ignore[arg-type]
        StubCloseClient(close_quantity="0.05"),
    )

    with pytest.raises(AppError) as exc:
        service.close_trade("a" * 36, TradeCloseRequest())
    assert exc.value.code == "TRADE_CLOSE_QUANTITY_MISMATCH"


def test_trade_management_rejects_unknown_trade() -> None:
    service = TradeManagementService(
        StubExecution(),  # type: ignore[arg-type]
        StubCloseClient(),
    )

    with pytest.raises(AppError) as exc:
        service.close_trade("z" * 36, TradeCloseRequest())
    assert exc.value.code == "TRADE_NOT_FOUND"
