"""Focused BE-14 exchange order audit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.persistence.database import Persistence
from app.persistence.models import Base, ExchangeOrderRow, FillRow
from app.persistence.repositories import TradingStateRepositories
from app.schemas.execution import (
    DemoProtectionState,
    DemoTradeCloseReason,
    DemoTradeLifecycle,
    DemoTradeRecord,
    DemoTradeRecordList,
)
from app.schemas.order_audit import OrderAuditRole, OrderAuditState
from app.schemas.scanner import ScannerDirection, ScannerSetup
from app.services.order_audit import OrderAuditService
from app.services.recovery import AutomationRecoveryGate

NOW = datetime(2026, 7, 19, 18, 0, tzinfo=UTC)
FILL_TIME = int(NOW.timestamp() * 1000)
ServiceFixture = tuple[
    OrderAuditService,
    "StubOrderAuditClient",
    TradingStateRepositories,
    AutomationRecoveryGate,
]


def _trade(
    *,
    lifecycle: DemoTradeLifecycle = DemoTradeLifecycle.OPEN,
    closed_reason: DemoTradeCloseReason | None = None,
    protective_exit_filled_quantity: Decimal = Decimal("0"),
) -> DemoTradeRecord:
    return DemoTradeRecord(
        trade_id="trade-be14",
        signal_id="2" * 64,
        symbol="BTCUSDT",
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        lifecycle=lifecycle,
        protection_state=DemoProtectionState.PROTECTED,
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("98"),
        take_profit_price=Decimal("104"),
        exchange_order_id="entry-1",
        client_order_id="af-entry-1",
        stop_order_id="stop-algo-1",
        stop_client_order_id="af-stop-1",
        take_profit_order_id="take-algo-1",
        take_profit_client_order_id="af-take-1",
        requested_quantity=Decimal("0.1"),
        executed_quantity=Decimal("0.1"),
        remaining_quantity=(
            Decimal("0") if lifecycle is DemoTradeLifecycle.CLOSED else Decimal("0.1")
        ),
        protective_exit_filled_quantity=protective_exit_filled_quantity,
        order_status="FILLED",
        tracked_margin_usdt=Decimal("10"),
        opened_at=NOW,
        closed_at=NOW if lifecycle is DemoTradeLifecycle.CLOSED else None,
        closed_reason=closed_reason,
        updated_at=NOW,
    )


class StubTradeSource:
    def __init__(self, trade: DemoTradeRecord) -> None:
        self.trade = trade

    def trades(self) -> DemoTradeRecordList:
        return DemoTradeRecordList(count=1, trades=[self.trade])


class StubOrderAuditClient:
    def __init__(self) -> None:
        self.entry: dict[str, Any] = {
            "symbol": "BTCUSDT",
            "clientOrderId": "af-entry-1",
            "orderId": "entry-1",
            "status": "FILLED",
            "origQty": "0.1",
            "executedQty": "0.1",
            "avgPrice": "100",
            "side": "BUY",
        }
        self.stop: dict[str, Any] = {
            "symbol": "BTCUSDT",
            "clientOrderId": "af-stop-1",
            "orderId": "stop-algo-1",
            "status": "NEW",
            "quantity": "0.1",
            "executedQty": "0",
            "side": "SELL",
        }
        self.take: dict[str, Any] = {
            "symbol": "BTCUSDT",
            "clientOrderId": "af-take-1",
            "orderId": "take-algo-1",
            "status": "NEW",
            "quantity": "0.1",
            "executedQty": "0",
            "side": "SELL",
        }
        self.manual: dict[str, Any] = {
            "symbol": "BTCUSDT",
            "clientOrderId": f"af-m-{'2' * 20}",
            "orderId": "manual-1",
            "status": "FILLED",
            "origQty": "0.1",
            "executedQty": "0.1",
            "avgPrice": "102",
            "side": "SELL",
        }
        self.fills = [
            _fill("entry-1", "entry-fill-1", "0.05", "99", "BUY"),
            _fill("entry-1", "entry-fill-2", "0.05", "101", "BUY"),
        ]

    def query_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]:
        assert symbol == "BTCUSDT"
        if orig_client_order_id == "af-entry-1":
            return dict(self.entry)
        assert orig_client_order_id == f"af-m-{'2' * 20}"
        return dict(self.manual)

    def query_algo_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]:
        assert symbol == "BTCUSDT"
        if orig_client_order_id == "af-stop-1":
            return dict(self.stop)
        assert orig_client_order_id == "af-take-1"
        return dict(self.take)

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


def _fill(
    order_id: str,
    fill_id: str,
    quantity: str,
    price: str,
    side: str,
) -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "orderId": order_id,
        "id": fill_id,
        "side": side,
        "qty": quantity,
        "price": price,
        "time": FILL_TIME,
    }


def _repositories(tmp_path: Path) -> TradingStateRepositories:
    tmp_path.mkdir(parents=True, exist_ok=True)
    persistence = Persistence(f"sqlite+pysqlite:///{tmp_path / 'be14.db'}")
    Base.metadata.create_all(persistence.engine)
    return TradingStateRepositories(persistence)


def _gate() -> AutomationRecoveryGate:
    gate = AutomationRecoveryGate()
    gate.begin()
    gate.mark_exchange_reconciled()
    gate.mark_signals_revalidated()
    gate.mark_ready()
    return gate


def _service(
    tmp_path: Path,
    trade: DemoTradeRecord,
    client: StubOrderAuditClient | None = None,
) -> ServiceFixture:
    resolved_client = client or StubOrderAuditClient()
    repositories = _repositories(tmp_path)
    gate = _gate()
    service = OrderAuditService(
        StubTradeSource(trade),
        resolved_client,
        repositories,
        gate,
        now_provider=lambda: NOW,
    )
    return service, resolved_client, repositories, gate


def _record(service: OrderAuditService, role: OrderAuditRole) -> Any:
    return next(item for item in service.records().records if item.role is role)


def test_baseline_records_entry_and_protective_fields(tmp_path: Path) -> None:
    service, _, repositories, gate = _service(tmp_path, _trade())

    report = service.reconcile()
    entry = _record(service, OrderAuditRole.ENTRY)

    assert report.state is OrderAuditState.READY
    assert (report.entry_order_count, report.protective_order_count) == (1, 2)
    assert report.audited_order_count == 3
    assert gate.snapshot().automation_ready is True
    assert entry.exchange_order_id == entry.actual_order_id == "entry-1"
    assert entry.client_order_id == "af-entry-1"
    assert entry.requested_quantity == entry.executed_quantity == Decimal("0.1")
    assert entry.average_fill_price == Decimal("100")
    assert entry.final_status == "FILLED"
    assert entry.exchange_trade_ids == ["entry-fill-1", "entry-fill-2"]
    with repositories.persistence.transaction() as session:
        fills = list(session.query(FillRow).order_by(FillRow.exchange_trade_id))
        assert len(fills) == 2


def test_protective_partial_then_terminal_progression_is_idempotent(
    tmp_path: Path,
) -> None:
    service, client, repositories, _ = _service(tmp_path, _trade())
    service.reconcile()
    client.stop.update(
        {
            "status": "PARTIALLY_FILLED",
            "actualOrderId": "stop-actual-1",
            "executedQty": "0.04",
            "avgPrice": "99",
        }
    )
    client.fills.append(_fill("stop-actual-1", "stop-fill-1", "0.04", "99", "SELL"))

    first = service.reconcile()
    second = service.reconcile()
    stop = _record(service, OrderAuditRole.STOP_LOSS)

    assert first.state is second.state is OrderAuditState.READY
    assert stop.actual_order_id == "stop-actual-1"
    assert stop.executed_quantity == Decimal("0.04")
    assert stop.average_fill_price == Decimal("99")
    assert stop.final_status == "PARTIALLY_FILLED"

    client.stop.update({"status": "FINISHED", "executedQty": "0.1", "avgPrice": "98.4"})
    client.fills.append(_fill("stop-actual-1", "stop-fill-2", "0.06", "98", "SELL"))

    terminal = service.reconcile()
    stop = _record(service, OrderAuditRole.STOP_LOSS)

    assert terminal.state is OrderAuditState.READY
    assert stop.executed_quantity == Decimal("0.1")
    assert stop.average_fill_price == Decimal("98.4")
    assert stop.final_status == "FINISHED"
    assert stop.exchange_trade_ids == ["stop-fill-1", "stop-fill-2"]
    with repositories.persistence.transaction() as session:
        rows = session.query(FillRow).filter_by(order_id="stop:stop-algo-1").all()
        assert len(rows) == 2


def test_regression_fails_closed_without_overwrite(tmp_path: Path) -> None:
    service, client, _, gate = _service(tmp_path, _trade())
    service.reconcile()
    client.stop.update(
        {
            "status": "PARTIALLY_FILLED",
            "actualOrderId": "stop-actual-1",
            "executedQty": "0.04",
            "avgPrice": "99",
        }
    )
    client.fills.append(_fill("stop-actual-1", "stop-fill-1", "0.04", "99", "SELL"))
    service.reconcile()
    client.stop.update({"status": "NEW", "executedQty": "0", "avgPrice": "0"})
    client.fills = [item for item in client.fills if item["orderId"] != "stop-actual-1"]

    blocked = service.reconcile()
    stop = _record(service, OrderAuditRole.STOP_LOSS)

    assert blocked.state is OrderAuditState.BLOCKED
    assert {item.code for item in blocked.findings} == {"ORDER_AUDIT_EXECUTED_QUANTITY_REGRESSION"}
    assert gate.snapshot().automation_ready is False
    assert stop.executed_quantity == Decimal("0.04")
    assert stop.final_status == "PARTIALLY_FILLED"


def test_legacy_row_is_canonicalized_from_exchange_evidence(tmp_path: Path) -> None:
    trade = _trade()
    service, _, repositories, _ = _service(tmp_path, trade)
    with repositories.persistence.transaction() as session:
        session.add(
            ExchangeOrderRow(
                order_id="stop:stop-algo-1",
                signal_id=trade.signal_id,
                trade_id=trade.trade_id,
                client_order_id=trade.stop_client_order_id,
                exchange_order_id=trade.stop_order_id,
                symbol=trade.symbol,
                status="NEW",
                quantity_text="0.1",
                average_price_text=None,
                payload_json='{"source":"legacy"}',
                created_at=NOW,
                updated_at=NOW,
            )
        )

    report = service.reconcile()
    stop = _record(service, OrderAuditRole.STOP_LOSS)

    assert report.state is OrderAuditState.READY
    assert stop.requested_quantity == Decimal("0.1")
    assert stop.executed_quantity == Decimal("0")
    assert stop.average_fill_price is None
    assert stop.final_status == "NEW"


def test_manual_close_records_complete_order_fields(tmp_path: Path) -> None:
    trade = _trade(
        lifecycle=DemoTradeLifecycle.CLOSED,
        closed_reason=DemoTradeCloseReason.MANUAL_CLOSE,
    )
    service, client, _, _ = _service(tmp_path, trade)
    client.stop["status"] = "CANCELED"
    client.take["status"] = "CANCELED"
    client.fills.append(_fill("manual-1", "manual-fill-1", "0.1", "102", "SELL"))

    report = service.reconcile()
    manual = _record(service, OrderAuditRole.MANUAL_CLOSE)

    assert report.state is OrderAuditState.READY
    assert report.manual_close_order_count == 1
    assert manual.exchange_order_id == "manual-1"
    assert manual.client_order_id == f"af-m-{'2' * 20}"
    assert manual.requested_quantity == manual.executed_quantity == Decimal("0.1")
    assert manual.average_fill_price == Decimal("102")
    assert manual.final_status == "FILLED"


def test_missing_fill_and_unavailable_source_fail_closed(tmp_path: Path) -> None:
    service, client, _, _ = _service(tmp_path, _trade())
    client.fills = []

    missing = service.reconcile()

    assert missing.state is OrderAuditState.BLOCKED
    assert {item.code for item in missing.findings} == {"ORDER_AUDIT_EXECUTED_QUANTITY_MISMATCH"}

    repositories = _repositories(tmp_path / "unavailable")
    gate = _gate()
    unavailable_service = OrderAuditService(
        StubTradeSource(_trade()),
        None,
        repositories,
        gate,
        now_provider=lambda: NOW,
    )

    unavailable = unavailable_service.reconcile()

    assert unavailable.state is OrderAuditState.UNAVAILABLE
    assert unavailable.blocking is True
    assert unavailable.findings[0].code == "ORDER_AUDIT_DURABILITY_UNAVAILABLE"
    assert unavailable_service.latest() == unavailable
    assert gate.snapshot().automation_ready is False
