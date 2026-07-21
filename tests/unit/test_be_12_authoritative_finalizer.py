"""Regression proof for competing BE-12 manual-close finalizers."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.schemas.execution import (
    DemoProtectionState,
    DemoTradeCloseReason,
    DemoTradeLifecycle,
    DemoTradeRecord,
)
from app.schemas.scanner import ScannerDirection, ScannerSetup
from app.schemas.trade_management import (
    ManualCloseIntentSnapshot,
    ManualCloseIntentState,
    TradeCloseRequest,
)
from app.services.durable_trade_management import DurableTradeManagementService

NOW = datetime(2026, 7, 19, 17, 5, tzinfo=UTC)


def _open_trade() -> DemoTradeRecord:
    return DemoTradeRecord(
        trade_id="a" * 36,
        signal_id="1" * 64,
        symbol="BTCUSDT",
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        lifecycle=DemoTradeLifecycle.OPEN,
        protection_state=DemoProtectionState.PROTECTED,
        entry_price=Decimal("101"),
        stop_loss_price=Decimal("99"),
        take_profit_price=Decimal("105"),
        exchange_order_id="entry-1",
        client_order_id="af-entry-1",
        stop_order_id="stop-1",
        stop_client_order_id="af-stop-1",
        take_profit_order_id="take-1",
        take_profit_client_order_id="af-take-1",
        requested_quantity=Decimal("0.1"),
        executed_quantity=Decimal("0.1"),
        order_status="FILLED",
        tracked_margin_usdt=Decimal("10.1"),
        opened_at=NOW,
        updated_at=NOW,
    )


def _winning_trade(trade: DemoTradeRecord) -> DemoTradeRecord:
    return trade.model_copy(
        update={
            "lifecycle": DemoTradeLifecycle.CLOSED,
            "exit_price": Decimal("104"),
            "realized_pnl_usdt": Decimal("0.29"),
            "gross_realized_pnl_usdt": Decimal("0.30"),
            "commission_usdt": Decimal("-0.01"),
            "order_status": "FILLED",
            "closed_at": NOW,
            "closed_reason": DemoTradeCloseReason.INVALIDATED,
            "updated_at": NOW,
        }
    )


class StubExecution:
    def __init__(self, trade: DemoTradeRecord) -> None:
        self.trade = trade
        self.store_calls = 0

    def get_trade(self, trade_id: str) -> DemoTradeRecord | None:
        return self.trade if trade_id == self.trade.trade_id else None

    def store_trade(self, trade: DemoTradeRecord) -> DemoTradeRecord:
        self.store_calls += 1
        self.trade = trade
        return trade


class StubCloseClient:
    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "orderId": "close-9001",
            "clientOrderId": orig_client_order_id,
            "status": "FILLED",
            "executedQty": "0.1",
            "avgPrice": "105",
        }

    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str,
        new_client_order_id: str,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        raise AssertionError("Existing competing close order must be queried, not submitted")

    def cancel_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        return {"symbol": symbol, "clientOrderId": orig_client_order_id}

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return [
            {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "0.4"},
            {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-0.01"},
        ]


class CompetingCompletionDurability:
    def __init__(self, winner: DemoTradeRecord) -> None:
        self.winner = winner
        self.candidate: DemoTradeRecord | None = None

    def prepare(
        self,
        trade: DemoTradeRecord,
        client_order_id: str,
    ) -> ManualCloseIntentSnapshot:
        return self._snapshot(ManualCloseIntentState.PENDING, client_order_id)

    def record_exchange_result(
        self,
        trade: DemoTradeRecord,
        client_order_id: str,
        payload: dict[str, Any],
    ) -> ManualCloseIntentSnapshot:
        return self._snapshot(ManualCloseIntentState.FILLED, client_order_id)

    def complete(
        self,
        trade: DemoTradeRecord,
        client_order_id: str,
    ) -> ManualCloseIntentSnapshot:
        self.candidate = trade
        return self._snapshot(
            ManualCloseIntentState.COMPLETED,
            client_order_id,
            completed_trade=self.winner,
        )

    def mark_recovery_required(
        self,
        trade: DemoTradeRecord,
        client_order_id: str,
        reason: str,
    ) -> ManualCloseIntentSnapshot:
        return self._snapshot(ManualCloseIntentState.RECOVERY_REQUIRED, client_order_id)

    def _snapshot(
        self,
        state: ManualCloseIntentState,
        client_order_id: str,
        *,
        completed_trade: DemoTradeRecord | None = None,
    ) -> ManualCloseIntentSnapshot:
        return ManualCloseIntentSnapshot(
            intent_id="2" * 64,
            trade_id=self.winner.trade_id,
            client_order_id=client_order_id,
            state=state,
            exchange_order_id="close-9001",
            order_status="FILLED",
            completed_trade=completed_trade,
            updated_at=NOW,
        )


def test_competing_finalizer_stores_and_returns_only_durable_winner() -> None:
    original = _open_trade()
    winner = _winning_trade(original)
    execution = StubExecution(original)
    durability = CompetingCompletionDurability(winner)
    service = DurableTradeManagementService(
        execution,  # type: ignore[arg-type]
        StubCloseClient(),
        durability,  # type: ignore[arg-type]
        now_provider=lambda: NOW,
    )

    result = service.close_trade(original.trade_id, TradeCloseRequest())

    assert durability.candidate is not None
    assert durability.candidate != winner
    assert result == winner
    assert execution.trade == winner
    assert execution.store_calls == 1
