"""BE-12 durable and idempotent manual-close verification."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.core.errors import AppError
from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.persistence.database import Persistence
from app.persistence.models import Base, ExchangeOrderRow, ExecutionIntentRow
from app.persistence.repositories import TradingStateRepositories
from app.schemas.execution import (
    DemoProtectionState,
    DemoTradeLifecycle,
    DemoTradeRecord,
)
from app.schemas.scanner import ScannerDirection, ScannerSetup
from app.schemas.trade_management import ManualCloseIntentState, TradeCloseRequest
from app.services.durable_trade_management import DurableTradeManagementService
from app.services.manual_close_durability import ManualCloseDurabilityService

NOW = datetime(2026, 7, 19, 16, 45, tzinfo=UTC)


def _trade() -> DemoTradeRecord:
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


def _closed_trade(trade: DemoTradeRecord, realized_pnl: Decimal) -> DemoTradeRecord:
    return trade.model_copy(
        update={
            "lifecycle": DemoTradeLifecycle.CLOSED,
            "exit_price": Decimal("105"),
            "realized_pnl_usdt": realized_pnl,
            "gross_realized_pnl_usdt": Decimal("0.4"),
            "commission_usdt": Decimal("-0.01"),
            "order_status": "FILLED",
            "closed_at": NOW,
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
    def __init__(
        self,
        *,
        existing_order: dict[str, Any] | None = None,
        fail_income: bool = False,
    ) -> None:
        self.order = existing_order
        self.fail_income = fail_income
        self.place_count = 0
        self.query_count = 0
        self.cancelled: list[str] = []

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        self.query_count += 1
        if self.order is None:
            raise BinanceDemoPrivateClientError(
                "Order does not exist",
                exchange_code=-2013,
            )
        return dict(self.order)

    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str,
        new_client_order_id: str,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        self.place_count += 1
        self.order = {
            "symbol": symbol,
            "side": side,
            "orderId": "close-9001",
            "clientOrderId": new_client_order_id,
            "status": "FILLED",
            "origQty": quantity,
            "executedQty": quantity,
            "avgPrice": "105",
            "reduceOnly": reduce_only,
        }
        return dict(self.order)

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
        if self.fail_income:
            raise BinanceDemoPrivateClientError("Income unavailable")
        return [
            {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "0.4"},
            {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-0.01"},
        ]


def _repositories(tmp_path: Path) -> TradingStateRepositories:
    persistence = Persistence(f"sqlite+pysqlite:///{tmp_path / 'be12.db'}")
    Base.metadata.create_all(persistence.engine)
    return TradingStateRepositories(persistence)


def _service(
    execution: StubExecution,
    client: StubCloseClient,
    repositories: TradingStateRepositories,
) -> DurableTradeManagementService:
    return DurableTradeManagementService(
        execution,  # type: ignore[arg-type]
        client,
        ManualCloseDurabilityService(repositories, now_provider=lambda: NOW),
        now_provider=lambda: NOW,
    )


def test_manual_close_persists_and_replays_one_completed_outcome(tmp_path: Path) -> None:
    repositories = _repositories(tmp_path)
    execution = StubExecution(_trade())
    client = StubCloseClient()
    service = _service(execution, client, repositories)

    closed = service.close_trade(execution.trade.trade_id, TradeCloseRequest())
    replayed = service.close_trade(execution.trade.trade_id, TradeCloseRequest())

    assert closed.lifecycle is DemoTradeLifecycle.CLOSED
    assert closed.realized_pnl_usdt == Decimal("0.39")
    assert replayed == closed
    assert client.place_count == 1
    assert client.query_count == 1

    intent_id = ManualCloseDurabilityService.intent_id(closed.trade_id)
    order_id = ManualCloseDurabilityService.order_row_id(closed.trade_id)
    assert len(order_id) == 64
    assert order_id != intent_id
    with repositories.persistence.transaction() as session:
        intent = session.get(ExecutionIntentRow, intent_id)
        order = session.get(ExchangeOrderRow, order_id)
        assert intent is not None
        assert intent.state == ManualCloseIntentState.COMPLETED.value
        assert order is not None
        assert order.exchange_order_id == "close-9001"
        assert order.status == "FILLED"
        assert order.quantity_text == "0.1"
        assert order.average_price_text == "105"


def test_first_completed_outcome_cannot_be_overwritten(tmp_path: Path) -> None:
    repositories = _repositories(tmp_path)
    trade = _trade()
    client_order_id = "af-m-" + trade.signal_id[:20]
    durability = ManualCloseDurabilityService(repositories, now_provider=lambda: NOW)
    durability.prepare(trade, client_order_id)
    durability.record_exchange_result(
        trade,
        client_order_id,
        {
            "orderId": "close-9001",
            "clientOrderId": client_order_id,
            "status": "FILLED",
            "executedQty": "0.1",
            "avgPrice": "105",
        },
    )

    first_trade = _closed_trade(trade, Decimal("0.39"))
    conflicting_trade = _closed_trade(trade, Decimal("9.99"))
    first = durability.complete(first_trade, client_order_id)
    second = durability.complete(conflicting_trade, client_order_id)

    assert first.completed_trade == first_trade
    assert second.completed_trade == first_trade
    assert second.completed_trade != conflicting_trade


def test_restart_recovers_filled_close_without_duplicate_submission(tmp_path: Path) -> None:
    repositories = _repositories(tmp_path)
    first_execution = StubExecution(_trade())
    first_client = StubCloseClient(fail_income=True)
    first_service = _service(first_execution, first_client, repositories)

    with pytest.raises(AppError) as exc:
        first_service.close_trade(first_execution.trade.trade_id, TradeCloseRequest())
    assert exc.value.code == "DEMO_INCOME_RECONCILIATION_FAILED"
    assert first_client.place_count == 1

    intent_id = ManualCloseDurabilityService.intent_id(first_execution.trade.trade_id)
    with repositories.persistence.transaction() as session:
        intent = session.get(ExecutionIntentRow, intent_id)
        assert intent is not None
        assert intent.state == ManualCloseIntentState.RECOVERY_REQUIRED.value

    restarted_execution = StubExecution(_trade())
    restarted_client = StubCloseClient(existing_order=first_client.order)
    restarted_service = _service(
        restarted_execution,
        restarted_client,
        repositories,
    )
    closed = restarted_service.close_trade(
        restarted_execution.trade.trade_id,
        TradeCloseRequest(),
    )

    assert closed.lifecycle is DemoTradeLifecycle.CLOSED
    assert restarted_client.query_count == 1
    assert restarted_client.place_count == 0
    with repositories.persistence.transaction() as session:
        intent = session.get(ExecutionIntentRow, intent_id)
        assert intent is not None
        assert intent.state == ManualCloseIntentState.COMPLETED.value


def test_second_instance_replays_completed_trade_without_exchange_call(tmp_path: Path) -> None:
    repositories = _repositories(tmp_path)
    first_execution = StubExecution(_trade())
    first_client = StubCloseClient()
    first_service = _service(first_execution, first_client, repositories)
    closed = first_service.close_trade(first_execution.trade.trade_id, TradeCloseRequest())

    stale_execution = StubExecution(_trade())
    second_client = StubCloseClient()
    second_service = _service(stale_execution, second_client, repositories)
    replayed = second_service.close_trade(stale_execution.trade.trade_id, TradeCloseRequest())

    assert replayed == closed
    assert stale_execution.trade == closed
    assert second_client.query_count == 0
    assert second_client.place_count == 0


def test_manual_close_fails_closed_without_persistence() -> None:
    execution = StubExecution(_trade())
    service = DurableTradeManagementService(
        execution,  # type: ignore[arg-type]
        StubCloseClient(),
        None,
        now_provider=lambda: NOW,
    )

    with pytest.raises(AppError) as exc:
        service.close_trade(execution.trade.trade_id, TradeCloseRequest())
    assert exc.value.code == "MANUAL_CLOSE_DURABILITY_UNAVAILABLE"
