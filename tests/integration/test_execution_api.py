"""Demo execution API integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from app.api.v1.dependencies import (
    get_execution_command_service,
    get_execution_service,
)
from app.core.errors import AppError
from app.main import create_app
from app.schemas.execution import (
    DemoAccountBalance,
    DemoExecutionAccountResponse,
    DemoExecutionPlan,
    DemoExecutionPlanList,
    DemoExecutionState,
    DemoExecutionStatusResponse,
    DemoExecutionSummary,
    DemoPlanState,
    DemoProtectionState,
    DemoTradeLifecycle,
    DemoTradeRecord,
    DemoTradeRecordList,
)
from app.schemas.risk import RiskDecision
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.signals import SignalLifecycle

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class StubExecution:
    def __init__(self) -> None:
        self.plan = DemoExecutionPlan(
            signal_id="a" * 64,
            symbol="BTCUSDT",
            direction=ScannerDirection.LONG,
            setup=ScannerSetup.TREND_PULLBACK,
            setup_name="Trend Pullback",
            signal_lifecycle=SignalLifecycle.ACTIVE,
            risk_decision=RiskDecision.APPROVED,
            plan_state=DemoPlanState.BLOCKED,
            grade=ScannerGrade.A_PLUS,
            score=92,
            confidence=80,
            entry_trigger_price=Decimal("101"),
            blocked_reason="EXECUTION_CONFIGURATION_LOCKED",
            executable_now=False,
            updated_at=NOW,
        )
        self.trade = DemoTradeRecord(
            trade_id="t" * 36,
            signal_id="b" * 64,
            symbol="ETHUSDT",
            direction=ScannerDirection.SHORT,
            setup=ScannerSetup.EMA_REJECTION,
            setup_name="EMA Rejection",
            lifecycle=DemoTradeLifecycle.OPEN,
            protection_state=DemoProtectionState.PROTECTED,
            grade=ScannerGrade.A,
            entry_price=Decimal("99"),
            stop_loss_price=Decimal("101"),
            take_profit_price=Decimal("95"),
            exchange_order_id="1001",
            client_order_id="af-entry-test",
            stop_order_id="1002",
            stop_client_order_id="af-stop-test",
            take_profit_order_id="1003",
            take_profit_client_order_id="af-take-test",
            requested_quantity=Decimal("0.10"),
            executed_quantity=Decimal("0.10"),
            order_status="FILLED",
            tracked_margin_usdt=Decimal("0"),
            unrealized_pnl_usdt=Decimal("0"),
            opened_at=NOW,
            updated_at=NOW,
        )

    def status(self) -> DemoExecutionStatusResponse:
        return DemoExecutionStatusResponse(
            state=DemoExecutionState.EXECUTION_LOCKED,
            execution_enabled=False,
            demo_credentials_configured=False,
            private_api_available=False,
            risk_engine_state="READY",
            take_profit_r_multiple=Decimal("2"),
            max_open_trades_limit=4,
            tracked_trade_count=1,
            available_tracking_slots=3,
            combined_unrealized_pnl_usdt=Decimal("0"),
            total_tracked_margin_usdt=Decimal("0"),
            updated_at=NOW,
            summary=DemoExecutionSummary(blocked_plans=1, open_trades=1, short_demo=1),
        )

    def account(self) -> DemoExecutionAccountResponse:
        return DemoExecutionAccountResponse(
            demo_private_execution_ready=True,
            can_trade=True,
            updated_at=NOW,
            total_wallet_balance_usdt=Decimal("100"),
            available_balance_usdt=Decimal("80"),
            total_unrealized_pnl_usdt=Decimal("0"),
            balances=[
                DemoAccountBalance(
                    asset="USDT",
                    wallet_balance=Decimal("100"),
                    available_balance=Decimal("80"),
                    unrealized_pnl=Decimal("0"),
                )
            ],
            open_positions=[],
        )

    def plans(self) -> DemoExecutionPlanList:
        return DemoExecutionPlanList(count=1, plans=[self.plan])

    def trades(self) -> DemoTradeRecordList:
        return DemoTradeRecordList(count=1, trades=[self.trade])


class StubCommands:
    def enqueue(self, signal_id: str) -> None:
        raise AppError(
            status_code=409,
            code="EXECUTION_DISABLED",
            message="Durable execution command remains configuration-locked",
        )


def test_execution_api_contract(settings) -> None:  # type: ignore[no-untyped-def]
    stub = StubExecution()
    app = create_app(settings)
    app.dependency_overrides[get_execution_service] = lambda: stub
    app.dependency_overrides[get_execution_command_service] = lambda: StubCommands()
    with TestClient(app) as client:
        status = client.get("/api/v1/execution/demo/status")
        assert status.status_code == 200
        assert status.json()["state"] == "EXECUTION_LOCKED"
        account = client.get("/api/v1/execution/demo/account")
        assert account.status_code == 200
        assert account.json()["can_trade"] is True
        plans = client.get(
            "/api/v1/execution/demo/plans",
            params={
                "symbol": "btcusdt",
                "direction": "LONG",
                "setup": "trend_pullback",
                "grade": "A+",
                "lifecycle": "ACTIVE",
                "plan_state": "BLOCKED",
            },
        )
        assert plans.status_code == 200
        assert plans.json()["count"] == 1
        trades = client.get("/api/v1/execution/demo/trades")
        assert trades.status_code == 200
        assert trades.json()["count"] == 1
        activate = client.post("/api/v1/execution/demo/activate/" + ("a" * 64))
        assert activate.status_code == 409
        assert activate.json()["error"]["code"] == "EXECUTION_DISABLED"
        invalid_symbol = client.get(
            "/api/v1/execution/demo/plans",
            params={"symbol": "***"},
        )
        assert invalid_symbol.status_code == 422
