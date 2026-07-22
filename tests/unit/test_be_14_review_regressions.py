"""Regression proofs for BE-14 automated review findings."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routes.order_audit import router as order_audit_router
from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.persistence.models import ExchangeOrderRow, FillRow
from app.schemas.execution import DemoTradeCloseReason, DemoTradeLifecycle
from app.schemas.order_audit import OrderAuditRole, OrderAuditState
from app.schemas.protective_lifecycle import (
    ProtectiveLifecycleReport,
    ProtectiveLifecycleState,
)
from app.services.global_reconciliation import GlobalReconciliationSafetyService
from app.services.order_audit import OrderAuditVerificationError, _Fill
from app.services.order_audit_runtime import RuntimeOrderAuditService
from app.services.order_reconciliation import ContinuousOrderReconciliationService
from app.services.position_reconciliation import ContinuousPositionReconciliationService
from app.services.protective_lifecycle import ProtectiveLifecycleVerificationService
from app.services.restart_recovery import RestartRecoveryOwnershipService
from tests.unit.test_be_07_global_reconciliation import (
    _Order,
    _order,
    _Position,
    _position,
    _Restart,
    _restart,
)
from tests.unit.test_be_14_order_audit import (
    FILL_TIME,
    NOW,
    StubOrderAuditClient,
    StubTradeSource,
    _fill,
    _gate,
    _repositories,
    _trade,
)


class FailingOrderAuditClient(StubOrderAuditClient):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def user_trades(
        self,
        *,
        symbol: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if self.failed:
            raise BinanceDemoPrivateClientError("audit source unavailable")
        return super().user_trades(
            symbol=symbol,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            limit=limit,
        )


class StartupProtectiveAuthority:
    def __init__(
        self,
        source: StubTradeSource,
        client: StubOrderAuditClient,
        repositories: Any,
        gate: Any,
    ) -> None:
        self._trade_source = source
        self._client = client
        self._repositories = repositories
        self._gate = gate
        self.calls = 0

    def reconcile(self) -> ProtectiveLifecycleReport:
        self.calls += 1
        return ProtectiveLifecycleReport(
            state=ProtectiveLifecycleState.IN_SYNC,
            checked_at=NOW,
            open_trade_count=1,
            verified_event_count=0,
            partial_trade_count=0,
            closed_trade_count=0,
            blocking=False,
            findings=[],
            events=[],
        )


def test_global_startup_proof_runs_order_audit_without_http(tmp_path: Path) -> None:
    trade = _trade()
    source = StubTradeSource(trade)
    client = StubOrderAuditClient()
    repositories = _repositories(tmp_path)
    gate = _gate()
    protective = StartupProtectiveAuthority(source, client, repositories, gate)
    service = GlobalReconciliationSafetyService(
        cast(ContinuousOrderReconciliationService, _Order(_order())),
        cast(ContinuousPositionReconciliationService, _Position(_position())),
        cast(RestartRecoveryOwnershipService, _Restart(_restart())),
        gate,
        protective_service=cast(
            ProtectiveLifecycleVerificationService,
            protective,
        ),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()
    audit = service.order_audit_service()

    assert report.blocking is False
    assert audit is not None
    assert audit.latest().state is OrderAuditState.READY
    assert audit.records().count == 3
    assert protective.calls == 1


def test_orders_endpoint_keeps_stale_records_but_propagates_blocking_state(
    tmp_path: Path,
) -> None:
    client = FailingOrderAuditClient()
    service = RuntimeOrderAuditService(
        StubTradeSource(_trade()),
        client,
        _repositories(tmp_path),
        _gate(),
        now_provider=lambda: NOW,
    )
    assert service.reconcile().state is OrderAuditState.READY
    client.failed = True
    app = FastAPI()
    app.state.order_audit_service = service
    app.include_router(order_audit_router, prefix="/api/v1")

    with TestClient(app) as test_client:
        response = test_client.get("/api/v1/order-audit/orders")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    assert body["state"] == "BLOCKED"
    assert body["blocking"] is True
    assert {item["code"] for item in body["findings"]} == {"ORDER_AUDIT_EXCHANGE_UNAVAILABLE"}


def test_unfilled_finished_protective_sibling_is_valid(tmp_path: Path) -> None:
    client = StubOrderAuditClient()
    client.stop["status"] = "FINISHED"

    trade = _trade(
        lifecycle=DemoTradeLifecycle.CLOSED,
        closed_reason=DemoTradeCloseReason.TAKE_PROFIT,
        protective_exit_filled_quantity=Decimal("0.1"),
    )
    trade = trade.model_copy(update={"protective_exit_reason": DemoTradeCloseReason.TAKE_PROFIT})

    client.take.update(
        {
            "status": "FILLED",
            "executedQty": "0.1",
            "avgPrice": "104",
            "actualOrderId": "take-actual-1",
        }
    )
    client.fills.append(_fill("take-actual-1", "take-fill-1", "0.1", "104", "SELL"))

    service = RuntimeOrderAuditService(
        StubTradeSource(trade),
        client,
        _repositories(tmp_path),
        _gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()
    assert report.state is OrderAuditState.READY

    stop = next(item for item in service.records().records if item.role is OrderAuditRole.STOP_LOSS)

    assert stop.final_status == "FINISHED"
    assert stop.executed_quantity == Decimal("0")
    assert stop.average_fill_price is None
    assert stop.actual_order_id is None


def test_same_raw_trade_id_is_durable_for_two_symbols(tmp_path: Path) -> None:
    repositories = _repositories(tmp_path)
    with repositories.persistence.transaction() as session:
        for symbol, order_id in (
            ("BTCUSDT", "entry:btc-order"),
            ("ETHUSDT", "entry:eth-order"),
        ):
            session.add(
                ExchangeOrderRow(
                    order_id=order_id,
                    signal_id=None,
                    trade_id=None,
                    client_order_id=f"client-{symbol}",
                    exchange_order_id=f"exchange-{symbol}",
                    symbol=symbol,
                    status="FILLED",
                    quantity_text="1",
                    average_price_text="100",
                    payload_json="{}",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.flush()
        fill = _Fill(
            exchange_trade_id="42",
            quantity=Decimal("1"),
            price=Decimal("100"),
            filled_at=datetime.fromtimestamp(FILL_TIME / 1000, tz=UTC),
        )
        RuntimeOrderAuditService._persist_fill(session, "entry:btc-order", fill)
        RuntimeOrderAuditService._persist_fill(session, "entry:eth-order", fill)

    with repositories.persistence.transaction() as session:
        records = sorted((item.symbol, item.exchange_trade_id) for item in session.query(FillRow))

    assert records == [("BTCUSDT", "42"), ("ETHUSDT", "42")]


def test_sl_fully_filled_tp_finished_with_zero_fills(tmp_path: Path) -> None:
    client = StubOrderAuditClient()
    client.take["status"] = "FINISHED"

    trade = _trade(
        lifecycle=DemoTradeLifecycle.CLOSED,
        closed_reason=DemoTradeCloseReason.STOP_LOSS,
        protective_exit_filled_quantity=Decimal("0.1"),
    )
    trade = trade.model_copy(update={"protective_exit_reason": DemoTradeCloseReason.STOP_LOSS})

    client.stop.update(
        {
            "status": "FILLED",
            "executedQty": "0.1",
            "avgPrice": "98",
            "actualOrderId": "stop-actual-1",
        }
    )
    client.fills.append(_fill("stop-actual-1", "stop-fill-1", "0.1", "98", "SELL"))

    service = RuntimeOrderAuditService(
        StubTradeSource(trade),
        client,
        _repositories(tmp_path),
        _gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()
    assert report.state is OrderAuditState.READY

    take = next(
        item for item in service.records().records if item.role is OrderAuditRole.TAKE_PROFIT
    )

    assert take.final_status == "FINISHED"
    assert take.executed_quantity == Decimal("0")
    assert take.average_fill_price is None
    assert take.actual_order_id is None


def test_unfilled_finished_protective_sibling_fails_without_verified_opposite(
    tmp_path: Path,
) -> None:
    client = StubOrderAuditClient()
    client.stop["status"] = "FINISHED"

    # Trade is OPEN, meaning opposite is NOT filled/verified
    trade = _trade(lifecycle=DemoTradeLifecycle.OPEN)

    service = RuntimeOrderAuditService(
        StubTradeSource(trade),
        client,
        _repositories(tmp_path),
        _gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()
    assert report.state is OrderAuditState.BLOCKED
    assert any(item.code == "ORDER_AUDIT_TERMINAL_QUANTITY_INVALID" for item in report.findings)


def test_finished_with_nonzero_executed_qty_but_missing_fills_fails_closed(tmp_path: Path) -> None:
    client = StubOrderAuditClient()
    client.stop.update(
        {
            "status": "FINISHED",
            "executedQty": "0.1",
            "avgPrice": "98",
            "actualOrderId": "stop-actual-1",
        }
    )
    # Sibling has executedQty but client.fills does NOT have fills for stop-actual-1!

    trade = _trade(
        lifecycle=DemoTradeLifecycle.CLOSED,
        closed_reason=DemoTradeCloseReason.STOP_LOSS,
        protective_exit_filled_quantity=Decimal("0.1"),
    )
    trade = trade.model_copy(update={"protective_exit_reason": DemoTradeCloseReason.STOP_LOSS})

    service = RuntimeOrderAuditService(
        StubTradeSource(trade),
        client,
        _repositories(tmp_path),
        _gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()
    assert report.state is OrderAuditState.BLOCKED
    assert any(item.code == "ORDER_AUDIT_EXECUTED_QUANTITY_MISMATCH" for item in report.findings)


def test_finished_with_contradictory_actual_order_id_fails_closed(tmp_path: Path) -> None:
    client = StubOrderAuditClient()
    client.stop.update(
        {
            "status": "FINISHED",
            "executedQty": "0.1",
            "avgPrice": "98",
            "actualOrderId": None,  # Contradictory: executed > 0 but actualOrderId is None
        }
    )

    trade = _trade(
        lifecycle=DemoTradeLifecycle.CLOSED,
        closed_reason=DemoTradeCloseReason.STOP_LOSS,
        protective_exit_filled_quantity=Decimal("0.1"),
    )
    trade = trade.model_copy(update={"protective_exit_reason": DemoTradeCloseReason.STOP_LOSS})

    service = RuntimeOrderAuditService(
        StubTradeSource(trade),
        client,
        _repositories(tmp_path),
        _gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()
    assert report.state is OrderAuditState.BLOCKED
    assert any(
        item.code
        in {
            "ORDER_AUDIT_ACTUAL_ORDER_ID_MISSING",
            "ORDER_AUDIT_EXECUTED_QUANTITY_MISMATCH",
        }
        for item in report.findings
    )


def test_same_symbol_trade_id_idempotency_and_replay_validation(tmp_path: Path) -> None:
    repositories = _repositories(tmp_path)
    with repositories.persistence.transaction() as session:
        session.add(
            ExchangeOrderRow(
                order_id="entry:btc-order",
                signal_id=None,
                trade_id=None,
                client_order_id="client-BTCUSDT",
                exchange_order_id="exchange-BTCUSDT",
                symbol="BTCUSDT",
                status="FILLED",
                quantity_text="1",
                average_price_text="100",
                payload_json="{}",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()

        fill = _Fill(
            exchange_trade_id="7",
            quantity=Decimal("1"),
            price=Decimal("100"),
            filled_at=datetime.fromtimestamp(FILL_TIME / 1000, tz=UTC),
        )

        # 1. BTCUSDT trade ID 7 replay with identical evidence is idempotent (succeeds)
        RuntimeOrderAuditService._persist_fill(session, "entry:btc-order", fill)
        RuntimeOrderAuditService._persist_fill(session, "entry:btc-order", fill)

        # 2. BTCUSDT trade ID 7 replay with different quantity fails closed
        different_qty_fill = _Fill(
            exchange_trade_id="7",
            quantity=Decimal("2"),  # different quantity
            price=Decimal("100"),
            filled_at=datetime.fromtimestamp(FILL_TIME / 1000, tz=UTC),
        )
        with pytest.raises(OrderAuditVerificationError) as exc_info:
            RuntimeOrderAuditService._persist_fill(session, "entry:btc-order", different_qty_fill)
        assert exc_info.value.code == "ORDER_AUDIT_FILL_IDENTITY_CONFLICT"

        # 3. BTCUSDT trade ID 7 replay with different average price fails closed
        different_price_fill = _Fill(
            exchange_trade_id="7",
            quantity=Decimal("1"),
            price=Decimal("101"),  # different price
            filled_at=datetime.fromtimestamp(FILL_TIME / 1000, tz=UTC),
        )
        with pytest.raises(OrderAuditVerificationError) as exc_info:
            RuntimeOrderAuditService._persist_fill(session, "entry:btc-order", different_price_fill)
        assert exc_info.value.code == "ORDER_AUDIT_FILL_IDENTITY_CONFLICT"

        # 4. BTCUSDT trade ID 7 replay with different order identity fails closed
        session.add(
            ExchangeOrderRow(
                order_id="entry:btc-order-2",
                signal_id=None,
                trade_id=None,
                client_order_id="client-BTCUSDT-2",
                exchange_order_id="exchange-BTCUSDT-2",
                symbol="BTCUSDT",
                status="FILLED",
                quantity_text="1",
                average_price_text="100",
                payload_json="{}",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        with pytest.raises(OrderAuditVerificationError) as exc_info:
            RuntimeOrderAuditService._persist_fill(session, "entry:btc-order-2", fill)
        assert exc_info.value.code == "ORDER_AUDIT_FILL_IDENTITY_CONFLICT"
