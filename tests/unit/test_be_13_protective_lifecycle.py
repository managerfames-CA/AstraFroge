"""Focused BE-13 protective lifecycle verification tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.persistence.database import Persistence
from app.persistence.models import Base, ExecutionIntentRow, TradeRow
from app.persistence.repositories import TradingStateRepositories
from app.schemas.execution import (
    DemoProtectionState,
    DemoTradeCloseReason,
    DemoTradeLifecycle,
    DemoTradeRecord,
    DemoTradeRecordList,
)
from app.schemas.protective_lifecycle import ProtectiveLifecycleState
from app.schemas.scanner import ScannerDirection, ScannerSetup
from app.services.protective_lifecycle import ProtectiveLifecycleVerificationService
from app.services.recovery import AutomationRecoveryGate

NOW = datetime(2026, 7, 19, 17, 15, tzinfo=UTC)
FILL_TIME = int(NOW.timestamp() * 1000)


def _trade() -> DemoTradeRecord:
    return DemoTradeRecord(
        trade_id="trade-be13",
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
        stop_order_id="stop-algo-1",
        stop_client_order_id="af-stop-1",
        take_profit_order_id="take-algo-1",
        take_profit_client_order_id="af-take-1",
        requested_quantity=Decimal("0.1"),
        executed_quantity=Decimal("0.1"),
        order_status="FILLED",
        tracked_margin_usdt=Decimal("10.1"),
        opened_at=NOW,
        updated_at=NOW,
    )


class StubTradeSource:
    def __init__(self, trade: DemoTradeRecord) -> None:
        self.trade = trade
        self.store_calls = 0

    def trades(self) -> DemoTradeRecordList:
        return DemoTradeRecordList(count=1, trades=[self.trade])

    def store_trade(self, trade: DemoTradeRecord) -> DemoTradeRecord:
        self.store_calls += 1
        self.trade = trade
        return trade


class StubLifecycleClient:
    def __init__(
        self,
        *,
        position_quantity: str | None,
        stop_status: str = "NEW",
        stop_executed: str = "0",
        stop_actual_order_id: str | None = None,
        take_status: str = "NEW",
        take_executed: str = "0",
        take_actual_order_id: str | None = None,
        fills: list[dict[str, Any]] | None = None,
    ) -> None:
        self.position_quantity = position_quantity
        self.stop_status = stop_status
        self.stop_executed = stop_executed
        self.stop_actual_order_id = stop_actual_order_id
        self.take_status = take_status
        self.take_executed = take_executed
        self.take_actual_order_id = take_actual_order_id
        self.fills = list(fills or [])
        self.cancelled: list[str] = []

    def positions(self) -> list[dict[str, Any]]:
        if self.position_quantity is None:
            return []
        return [{"symbol": "BTCUSDT", "positionAmt": self.position_quantity}]

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        assert symbol == "BTCUSDT"
        assert orig_client_order_id == "af-entry-1"
        return {
            "symbol": symbol,
            "clientOrderId": orig_client_order_id,
            "orderId": "entry-1",
            "status": "FILLED",
            "executedQty": "0.1",
            "avgPrice": "101",
        }

    def query_algo_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]:
        assert symbol == "BTCUSDT"
        if orig_client_order_id == "af-stop-1":
            return self._algo(
                client_id=orig_client_order_id,
                algo_id="stop-algo-1",
                status=self.stop_status,
                executed=self.stop_executed,
                actual=self.stop_actual_order_id,
            )
        assert orig_client_order_id == "af-take-1"
        return self._algo(
            client_id=orig_client_order_id,
            algo_id="take-algo-1",
            status=self.take_status,
            executed=self.take_executed,
            actual=self.take_actual_order_id,
        )

    def cancel_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]:
        self.cancelled.append(orig_client_order_id)
        algo_id = (
            "stop-algo-1" if orig_client_order_id == "af-stop-1" else "take-algo-1"
        )
        return {
            "symbol": symbol,
            "clientOrderId": orig_client_order_id,
            "orderId": algo_id,
            "status": "CANCELED",
        }

    def user_trades(
        self,
        *,
        symbol: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        assert symbol == "BTCUSDT"
        assert start_time_ms < end_time_ms
        assert limit == 1000
        return list(self.fills)

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        assert start_time_ms < end_time_ms
        assert limit == 1000
        return [
            {
                "symbol": "BTCUSDT",
                "incomeType": "REALIZED_PNL",
                "income": "0.4",
                "asset": "USDT",
                "tranId": "income-pnl",
                "tradeId": "fill-take",
                "time": FILL_TIME,
            },
            {
                "symbol": "BTCUSDT",
                "incomeType": "COMMISSION",
                "income": "-0.01",
                "asset": "USDT",
                "tranId": "income-entry-fee",
                "tradeId": "fill-entry",
                "time": FILL_TIME,
            },
            {
                "symbol": "BTCUSDT",
                "incomeType": "COMMISSION",
                "income": "-0.01",
                "asset": "USDT",
                "tranId": "income-take-fee",
                "tradeId": "fill-take",
                "time": FILL_TIME,
            },
        ]

    @staticmethod
    def _algo(
        *,
        client_id: str,
        algo_id: str,
        status: str,
        executed: str,
        actual: str | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "symbol": "BTCUSDT",
            "clientOrderId": client_id,
            "orderId": algo_id,
            "status": status,
            "executedQty": executed,
        }
        if actual is not None:
            result["actualOrderId"] = actual
        return result


def _fill(order_id: str, fill_id: str, quantity: str, price: str) -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "orderId": order_id,
        "id": fill_id,
        "side": "SELL",
        "qty": quantity,
        "price": price,
        "time": FILL_TIME,
    }


def _repositories(tmp_path: Path, trade: DemoTradeRecord) -> TradingStateRepositories:
    persistence = Persistence(f"sqlite+pysqlite:///{tmp_path / 'be13.db'}")
    Base.metadata.create_all(persistence.engine)
    repositories = TradingStateRepositories(persistence)
    with persistence.transaction() as session:
        session.add(
            TradeRow(
                trade_id=trade.trade_id,
                signal_id=trade.signal_id,
                lifecycle=trade.lifecycle.value,
                symbol=trade.symbol,
                quantity_text=format(trade.executed_quantity, "f"),
                entry_price_text=format(trade.entry_price, "f"),
                exit_price_text=None,
                realized_pnl_text="0",
                payload_json=trade.model_dump_json(),
                opened_at=trade.opened_at,
                closed_at=None,
                updated_at=trade.updated_at,
            )
        )
    return repositories


def _gate() -> AutomationRecoveryGate:
    gate = AutomationRecoveryGate()
    gate.begin()
    gate.mark_exchange_reconciled()
    gate.mark_signals_revalidated()
    gate.mark_ready()
    return gate


def test_no_protective_fill_keeps_verified_position_in_sync(tmp_path: Path) -> None:
    trade = _trade()
    source = StubTradeSource(trade)
    service = ProtectiveLifecycleVerificationService(
        source,
        StubLifecycleClient(position_quantity="0.1"),
        _repositories(tmp_path, trade),
        _gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()

    assert report.state is ProtectiveLifecycleState.IN_SYNC
    assert report.blocking is False
    assert report.events == []
    assert source.store_calls == 0


def test_partial_stop_fill_is_durable_idempotent_and_fail_closed(tmp_path: Path) -> None:
    trade = _trade()
    source = StubTradeSource(trade)
    repositories = _repositories(tmp_path, trade)
    gate = _gate()
    client = StubLifecycleClient(
        position_quantity="0.06",
        stop_status="PARTIALLY_FILLED",
        stop_executed="0.04",
        stop_actual_order_id="stop-actual-1",
        fills=[_fill("stop-actual-1", "fill-stop", "0.04", "99")],
    )
    service = ProtectiveLifecycleVerificationService(
        source,
        client,
        repositories,
        gate,
        now_provider=lambda: NOW,
    )

    first = service.reconcile()
    second = service.reconcile()

    assert first.state is ProtectiveLifecycleState.BLOCKED
    assert first.partial_trade_count == 1
    assert first.verified_event_count == 1
    assert first.events[0].event_type.value == "PARTIAL_CLOSE"
    assert second.verified_event_count == 0
    assert source.trade.lifecycle is DemoTradeLifecycle.OPEN
    assert source.trade.remaining_quantity == Decimal("0.06")
    assert source.trade.protective_exit_filled_quantity == Decimal("0.04")
    assert source.trade.protective_exit_fill_ids == ["fill-stop"]
    assert gate.snapshot().automation_ready is False
    with repositories.persistence.transaction() as session:
        events = list(
            session.query(ExecutionIntentRow).filter_by(
                operation="PROTECTIVE_LIFECYCLE"
            )
        )
        assert len(events) == 1


def test_full_take_profit_closes_from_verified_order_fill_position_and_income(
    tmp_path: Path,
) -> None:
    trade = _trade()
    source = StubTradeSource(trade)
    repositories = _repositories(tmp_path, trade)
    client = StubLifecycleClient(
        position_quantity=None,
        take_status="FINISHED",
        take_executed="0.1",
        take_actual_order_id="take-actual-1",
        fills=[
            _fill("entry-1", "fill-entry", "0.1", "101"),
            _fill("take-actual-1", "fill-take", "0.1", "105"),
        ],
    )
    service = ProtectiveLifecycleVerificationService(
        source,
        client,
        repositories,
        _gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()

    assert report.state is ProtectiveLifecycleState.CLOSED_VERIFIED
    assert report.blocking is False
    assert report.closed_trade_count == 1
    assert source.trade.lifecycle is DemoTradeLifecycle.CLOSED
    assert source.trade.closed_reason is DemoTradeCloseReason.TAKE_PROFIT
    assert source.trade.remaining_quantity == Decimal("0")
    assert source.trade.exit_price == Decimal("105")
    assert source.trade.gross_realized_pnl_usdt == Decimal("0.4")
    assert source.trade.realized_pnl_usdt == Decimal("0.38")
    assert source.trade.protective_sibling_cancelled is True
    assert client.cancelled == ["af-stop-1"]


def test_conflicting_stop_and_take_fills_fail_without_mutation(tmp_path: Path) -> None:
    trade = _trade()
    source = StubTradeSource(trade)
    repositories = _repositories(tmp_path, trade)
    service = ProtectiveLifecycleVerificationService(
        source,
        StubLifecycleClient(
            position_quantity=None,
            stop_status="PARTIALLY_FILLED",
            stop_executed="0.05",
            stop_actual_order_id="stop-actual-1",
            take_status="PARTIALLY_FILLED",
            take_executed="0.05",
            take_actual_order_id="take-actual-1",
            fills=[
                _fill("stop-actual-1", "fill-stop", "0.05", "99"),
                _fill("take-actual-1", "fill-take", "0.05", "105"),
            ],
        ),
        repositories,
        _gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()

    assert report.state is ProtectiveLifecycleState.BLOCKED
    assert {item.code for item in report.findings} == {"CONFLICTING_PROTECTIVE_FILLS"}
    assert source.store_calls == 0
    with repositories.persistence.transaction() as session:
        row = session.get(TradeRow, trade.trade_id)
        assert row is not None
        assert DemoTradeRecord.model_validate_json(row.payload_json) == trade


def test_position_reduction_without_matching_fill_fails_closed(tmp_path: Path) -> None:
    trade = _trade()
    source = StubTradeSource(trade)
    service = ProtectiveLifecycleVerificationService(
        source,
        StubLifecycleClient(position_quantity="0.06"),
        _repositories(tmp_path, trade),
        _gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()

    assert report.state is ProtectiveLifecycleState.BLOCKED
    assert {item.code for item in report.findings} == {"UNVERIFIED_POSITION_REDUCTION"}
    assert source.store_calls == 0
