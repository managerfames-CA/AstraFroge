"""Trade Management API integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from app.api.v1.active_trade_dependencies import get_active_trade_authority_service
from app.api.v1.manual_close_dependencies import (
    get_durable_trade_management_service,
)
from app.main import create_app
from app.schemas.execution import (
    DemoProtectionState,
    DemoTradeCloseReason,
    DemoTradeLifecycle,
    DemoTradeRecord,
)
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.trade_management import (
    ManagedTradeRecordList,
    TradeCloseRequest,
    TradeManagementState,
    TradeManagementStatusResponse,
    TradeManagementSummary,
)

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class StubTradeManagement:
    def __init__(self) -> None:
        self.trade = DemoTradeRecord(
            trade_id="a" * 36,
            signal_id="1" * 64,
            symbol="BTCUSDT",
            direction=ScannerDirection.LONG,
            setup=ScannerSetup.TREND_PULLBACK,
            setup_name="Trend Pullback",
            lifecycle=DemoTradeLifecycle.OPEN,
            protection_state=DemoProtectionState.PROTECTED,
            grade=ScannerGrade.A_PLUS,
            entry_price=Decimal("101"),
            stop_loss_price=Decimal("99"),
            take_profit_price=Decimal("105"),
            exchange_order_id="2001",
            client_order_id="af-entry-trade-test",
            stop_order_id="2002",
            stop_client_order_id="af-stop-trade-test",
            take_profit_order_id="2003",
            take_profit_client_order_id="af-take-trade-test",
            requested_quantity=Decimal("0.25"),
            executed_quantity=Decimal("0.25"),
            order_status="FILLED",
            tracked_margin_usdt=Decimal("25"),
            unrealized_pnl_usdt=Decimal("3.5"),
            exchange_position_verified=True,
            position_snapshot_id="snapshot-1",
            position_snapshot_at=NOW,
            position_source="BINANCE_DEMO_PRIVATE_API",
            exchange_position_quantity=Decimal("0.25"),
            opened_at=NOW,
            updated_at=NOW,
        )

    def status(self) -> TradeManagementStatusResponse:
        return TradeManagementStatusResponse(
            state=TradeManagementState.READY,
            exchange_authoritative_active_trades=True,
            execution_engine_state="EXECUTION_LOCKED",
            max_open_trades_limit=4,
            tracked_trade_count=1,
            open_trade_count=1,
            available_tracking_slots=3,
            local_open_candidate_count=1,
            exchange_open_position_count=1,
            position_snapshot_id="snapshot-1",
            position_snapshot_at=NOW,
            updated_at=NOW,
            summary=TradeManagementSummary(
                manual_demo_trades=1,
                long_demo=1,
                combined_unrealized_pnl_usdt=Decimal("3.5"),
                total_tracked_margin_usdt=Decimal("25"),
            ),
        )

    def trades(self, filters):  # type: ignore[no-untyped-def]
        return ManagedTradeRecordList(
            count=1,
            trades=[self.trade],
            exchange_authoritative_open_trades=True,
            local_open_candidate_count=1,
            exchange_open_position_count=1,
            position_snapshot_id="snapshot-1",
            position_snapshot_at=NOW,
        )

    def close_trade(self, trade_id: str, request: TradeCloseRequest) -> DemoTradeRecord:
        return self.trade.model_copy(
            update={
                "trade_id": trade_id,
                "lifecycle": DemoTradeLifecycle.CLOSED,
                "closed_reason": DemoTradeCloseReason(request.reason.value),
                "closed_at": NOW,
            }
        )


def test_trade_management_api_contract(settings) -> None:  # type: ignore[no-untyped-def]
    stub = StubTradeManagement()
    app = create_app(settings)
    app.dependency_overrides[get_active_trade_authority_service] = lambda: stub
    app.dependency_overrides[get_durable_trade_management_service] = lambda: stub
    with TestClient(app) as client:
        status = client.get("/api/v1/trade-management/status")
        assert status.status_code == 200
        assert status.json()["state"] == "READY"
        assert status.json()["exchange_authoritative_active_trades"] is True
        trades = client.get(
            "/api/v1/trade-management/trades",
            params={
                "symbol": "btcusdt",
                "direction": "LONG",
                "min_grade": "A",
                "include_closed": False,
                "sort_by": "OPENED_AT_DESC",
            },
        )
        assert trades.status_code == 200
        assert trades.json()["count"] == 1
        assert trades.json()["exchange_authoritative_open_trades"] is True
        assert trades.json()["trades"][0]["exchange_position_verified"] is True
        closed = client.post(
            "/api/v1/trade-management/close/" + ("a" * 36),
            json={"reason": "MANUAL_CLOSE"},
        )
        assert closed.status_code == 200
        invalid_symbol = client.get(
            "/api/v1/trade-management/trades",
            params={"symbol": "***"},
        )
        assert invalid_symbol.status_code == 422
