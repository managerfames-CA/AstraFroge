"""Binance Demo Execution Engine unit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.schemas.execution import (
    DemoExecutionActivateRequest,
    DemoExecutionState,
    DemoPlanState,
    DemoProtectionState,
    DemoTradeLifecycle,
)
from app.schemas.risk import (
    KillSwitchState,
    RiskAssessment,
    RiskAssessmentList,
    RiskDecision,
    RiskEngineState,
    RiskStatusResponse,
    RiskSummary,
)
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.signals import SignalLifecycle
from app.services.execution import DemoExecutionService

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
SIGNAL_ID = "a" * 64


class StubRisk:
    def __init__(self) -> None:
        self._assessments = [
            RiskAssessment(
                signal_id=SIGNAL_ID,
                symbol="BTCUSDT",
                direction=ScannerDirection.LONG,
                setup=ScannerSetup.TREND_PULLBACK,
                setup_name="Trend Pullback",
                signal_lifecycle=SignalLifecycle.ACTIVE,
                grade=ScannerGrade.A_PLUS,
                score=92,
                confidence=80,
                decision=RiskDecision.APPROVED,
                approved_for_execution=True,
                entry_trigger_price=Decimal("101"),
                stop_loss_price=Decimal("95"),
                stop_distance=Decimal("6"),
                risk_percent=Decimal("1"),
                risk_budget_usdt=Decimal("0.642"),
                recommended_quantity=Decimal("0.1078"),
                position_notional_usdt=Decimal("10.8878"),
                required_margin_usdt=Decimal("1.08878"),
                wallet_balance_usdt=Decimal("100"),
                available_balance_usdt=Decimal("90"),
                current_margin_exposure_usdt=Decimal("0"),
                max_open_trades_limit=4,
                updated_at=NOW,
            ),
            RiskAssessment(
                signal_id="b" * 64,
                symbol="ETHUSDT",
                direction=ScannerDirection.SHORT,
                setup=ScannerSetup.EMA_REJECTION,
                setup_name="EMA Rejection",
                signal_lifecycle=SignalLifecycle.WATCH,
                grade=ScannerGrade.B_PLUS,
                score=84,
                confidence=68,
                decision=RiskDecision.WATCH,
                approved_for_execution=False,
                entry_trigger_price=Decimal("99"),
                current_margin_exposure_usdt=Decimal("0"),
                max_open_trades_limit=4,
                updated_at=NOW,
            ),
        ]

    def status(self) -> RiskStatusResponse:
        return RiskStatusResponse(
            state=RiskEngineState.READY,
            signal_engine_state="READY",
            daily_loss_limit_percent=Decimal("3"),
            daily_profit_lock_percent=Decimal("5"),
            current_margin_exposure_usdt=Decimal("0"),
            max_open_trades_limit=4,
            available_tracking_slots=4,
            emergency_kill_switch=KillSwitchState.OFFLINE,
            updated_at=NOW,
            summary=RiskSummary(approved=1, watch=1),
        )

    def assessments(self) -> RiskAssessmentList:
        return RiskAssessmentList(count=len(self._assessments), assessments=self._assessments)


class StubDemoClient:
    def __init__(
        self,
        *,
        hedge_mode: bool = False,
        entry_avg_price: str = "101.5",
        fail_take_profit: bool = False,
        fail_emergency_close: bool = False,
    ) -> None:
        self.hedge_mode = hedge_mode
        self.entry_avg_price = entry_avg_price
        self.fail_take_profit = fail_take_profit
        self.fail_emergency_close = fail_emergency_close
        self.market_orders: list[dict[str, Any]] = []
        self.protective_orders: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    def position_mode(self) -> dict[str, Any]:
        return {"dualSidePosition": self.hedge_mode}

    def exchange_info(self) -> dict[str, Any]:
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "minPrice": "0.1",
                            "maxPrice": "1000000",
                            "tickSize": "0.1",
                        },
                        {
                            "filterType": "MARKET_LOT_SIZE",
                            "minQty": "0.001",
                            "maxQty": "1000",
                            "stepSize": "0.001",
                        },
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    def mark_price(self, symbol: str) -> dict[str, Any]:
        assert symbol == "BTCUSDT"
        return {"symbol": symbol, "markPrice": "101.5"}

    def account(self) -> dict[str, Any]:
        return {
            "canTrade": True,
            "totalWalletBalance": "100",
            "availableBalance": "90",
            "totalUnrealizedProfit": "0",
            "assets": [],
        }

    def positions(self) -> list[dict[str, Any]]:
        return []

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        raise BinanceDemoPrivateClientError(
            "Order does not exist",
            status_code=400,
            exchange_code=-2013,
        )

    def cancel_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        self.cancelled.append(orig_client_order_id)
        return {"symbol": symbol, "clientOrderId": orig_client_order_id}

    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str,
        new_client_order_id: str,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        request = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "new_client_order_id": new_client_order_id,
            "reduce_only": reduce_only,
        }
        self.market_orders.append(request)
        if reduce_only and self.fail_emergency_close:
            raise BinanceDemoPrivateClientError("Emergency close failed")
        return {
            "orderId": 987 if reduce_only else 123,
            "clientOrderId": new_client_order_id,
            "status": "FILLED",
            "executedQty": quantity,
            "avgPrice": "101.4" if reduce_only else self.entry_avg_price,
        }

    def place_protective_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str,
        stop_price: str,
        new_client_order_id: str,
    ) -> dict[str, Any]:
        request = {
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "quantity": quantity,
            "stop_price": stop_price,
            "new_client_order_id": new_client_order_id,
        }
        self.protective_orders.append(request)
        if order_type == "TAKE_PROFIT_MARKET" and self.fail_take_profit:
            raise BinanceDemoPrivateClientError("Take Profit rejected")
        return {
            "orderId": 456 if order_type == "STOP_MARKET" else 789,
            "clientOrderId": new_client_order_id,
            "status": "NEW",
        }


def _enabled_settings(*, take_profit_r: Decimal = Decimal("2")) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        execution_enabled=True,
        execution_take_profit_r_multiple=take_profit_r,
        binance_demo_base_url="https://demo-fapi.binance.example",
        binance_demo_api_key="demo-key",
        binance_demo_api_secret="demo-secret",
    )


def test_demo_execution_projects_policy_locked_plans() -> None:
    service = DemoExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        Settings(_env_file=None, environment="test"),
    )

    plans = service.plans()
    assert plans.plans[0].plan_state is DemoPlanState.BLOCKED
    assert plans.plans[0].blocked_reason == "TAKE_PROFIT_POLICY_NOT_CONFIGURED"
    assert plans.plans[1].plan_state is DemoPlanState.WATCH
    assert service.status().state is DemoExecutionState.EXECUTION_LOCKED


def test_demo_execution_rejects_client_quantity() -> None:
    service = DemoExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        StubDemoClient(),
    )

    with pytest.raises(AppError) as exc:
        service.activate(
            SIGNAL_ID,
            DemoExecutionActivateRequest(quantity=Decimal("99")),
        )
    assert exc.value.code == "CLIENT_QUANTITY_NOT_ALLOWED"


def test_demo_execution_rejects_hedge_mode() -> None:
    service = DemoExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        StubDemoClient(hedge_mode=True),
    )

    with pytest.raises(AppError) as exc:
        service.activate(SIGNAL_ID)
    assert exc.value.code == "HEDGE_MODE_UNSUPPORTED"


def test_demo_execution_opens_only_verified_protected_trade() -> None:
    client = StubDemoClient()
    service = DemoExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        client,
    )

    trade = service.activate(SIGNAL_ID)

    assert trade.lifecycle is DemoTradeLifecycle.OPEN
    assert trade.protection_state is DemoProtectionState.PROTECTED
    assert trade.entry_price == Decimal("101.5")
    assert trade.requested_quantity == Decimal("0.107")
    assert trade.executed_quantity == Decimal("0.107")
    assert trade.stop_loss_price == Decimal("95.0")
    assert trade.take_profit_price == Decimal("114.5")
    assert trade.exchange_order_id == "123"
    assert trade.stop_order_id == "456"
    assert trade.take_profit_order_id == "789"
    assert client.market_orders[0]["quantity"] == "0.107"
    assert [item["order_type"] for item in client.protective_orders] == [
        "STOP_MARKET",
        "TAKE_PROFIT_MARKET",
    ]


def test_missing_fill_price_is_rejected_and_position_is_closed() -> None:
    client = StubDemoClient(entry_avg_price="0")
    service = DemoExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        client,
    )

    with pytest.raises(AppError) as exc:
        service.activate(SIGNAL_ID)
    assert exc.value.code == "ENTRY_FILL_NOT_VERIFIED"
    assert len(client.market_orders) == 2
    assert client.market_orders[1]["reduce_only"] is True
    assert service.trades().count == 0


def test_take_profit_failure_closes_position_and_cancels_stop() -> None:
    client = StubDemoClient(fail_take_profit=True)
    service = DemoExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        client,
    )

    with pytest.raises(AppError) as exc:
        service.activate(SIGNAL_ID)
    assert exc.value.code == "PROTECTIVE_ORDER_FAILED_POSITION_CLOSED"
    assert client.cancelled == [f"af-s-{SIGNAL_ID[:20]}"]
    assert client.market_orders[-1]["reduce_only"] is True
    assert service.trades().count == 0


def test_unverified_emergency_close_reports_unprotected_position() -> None:
    client = StubDemoClient(
        fail_take_profit=True,
        fail_emergency_close=True,
    )
    service = DemoExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        client,
    )

    with pytest.raises(AppError) as exc:
        service.activate(SIGNAL_ID)
    assert exc.value.code == "UNPROTECTED_DEMO_POSITION"


def test_auto_execute_pending_opens_executable_plan_once() -> None:
    client = StubDemoClient()
    service = DemoExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        client,
    )

    first = service.auto_execute_pending()
    second = service.auto_execute_pending()

    assert first == 1
    assert second == 0
    assert service.trades().count == 1
    assert len(client.market_orders) == 1


def test_auto_execute_pending_skips_when_execution_locked() -> None:
    service = DemoExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        Settings(_env_file=None, environment="test"),
    )

    assert service.auto_execute_pending() == 0


def test_execution_service_reloads_persisted_trades(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store_path = tmp_path / "demo-trades.json"
    client = StubDemoClient()
    service = DemoExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        client,
        trade_store_path=store_path,
    )

    opened_trade = service.activate(SIGNAL_ID)
    reloaded = DemoExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        client,
        trade_store_path=store_path,
    )

    assert opened_trade.trade_id in {trade.trade_id for trade in reloaded.trades().trades}
    assert reloaded.trades().count == 1
