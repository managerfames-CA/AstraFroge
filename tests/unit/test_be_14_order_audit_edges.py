"""BE-14 fail-closed edge and durable-integrity verification."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routes import order_audit as order_audit_route
from app.persistence.models import ExchangeOrderRow
from app.schemas.execution import DemoTradeCloseReason, DemoTradeLifecycle
from app.schemas.order_audit import OrderAuditRole, OrderAuditState
from app.services.order_audit import (
    OrderAuditService,
    OrderAuditVerificationError,
    _Evidence,
    _Fill,
)
from app.services.order_audit_runtime import RuntimeOrderAuditService
from tests.unit.test_be_14_order_audit import (
    FILL_TIME,
    NOW,
    StubOrderAuditClient,
    StubTradeSource,
    _fill,
    _gate,
    _repositories,
    _service,
    _trade,
)


class UnexpectedClient(StubOrderAuditClient):
    def user_trades(
        self,
        *,
        symbol: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        raise ValueError("unexpected payload failure")


class TruncatedClient(StubOrderAuditClient):
    def user_trades(
        self,
        *,
        symbol: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return [
            {
                "symbol": "OTHER",
                "orderId": "other",
                "id": str(index),
                "side": "BUY",
                "qty": "1",
                "price": "1",
                "time": FILL_TIME,
            }
            for index in range(1000)
        ]


@pytest.mark.parametrize(
    (
        "role",
        "requested",
        "executed",
        "average",
        "status",
        "actual_order_id",
        "code",
    ),
    [
        (OrderAuditRole.ENTRY, "1", "0", None, "UNKNOWN", "1", "ORDER_AUDIT_STATUS_INVALID"),
        (OrderAuditRole.ENTRY, "1", "2", "100", "FILLED", "1", "ORDER_AUDIT_OVERFILL"),
        (
            OrderAuditRole.ENTRY,
            "1",
            "0.5",
            None,
            "PARTIALLY_FILLED",
            "1",
            "ORDER_AUDIT_AVERAGE_PRICE_MISSING",
        ),
        (
            OrderAuditRole.ENTRY,
            "1",
            "0",
            "100",
            "NEW",
            "1",
            "ORDER_AUDIT_AVERAGE_PRICE_WITHOUT_FILL",
        ),
        (
            OrderAuditRole.STOP_LOSS,
            "1",
            "0.5",
            "100",
            "PARTIALLY_FILLED",
            None,
            "ORDER_AUDIT_ACTUAL_ORDER_ID_MISSING",
        ),
        (
            OrderAuditRole.ENTRY,
            "1",
            "0.5",
            "100",
            "NEW",
            "1",
            "ORDER_AUDIT_NEW_STATUS_HAS_FILL",
        ),
        (
            OrderAuditRole.ENTRY,
            "1",
            "0",
            None,
            "PARTIALLY_FILLED",
            "1",
            "ORDER_AUDIT_PARTIAL_STATUS_INVALID",
        ),
        (
            OrderAuditRole.ENTRY,
            "1",
            "0.5",
            "100",
            "FILLED",
            "1",
            "ORDER_AUDIT_TERMINAL_QUANTITY_INVALID",
        ),
        (
            OrderAuditRole.ENTRY,
            "1",
            "0.5",
            "100",
            "FINISHED",
            "1",
            "ORDER_AUDIT_TERMINAL_QUANTITY_INVALID",
        ),
        (
            OrderAuditRole.ENTRY,
            "1",
            "0.5",
            "100",
            "REJECTED",
            "1",
            "ORDER_AUDIT_REJECTED_STATUS_HAS_FILL",
        ),
    ],
)
def test_runtime_economics_rejects_invalid_exchange_states(
    role: OrderAuditRole,
    requested: str,
    executed: str,
    average: str | None,
    status: str,
    actual_order_id: str | None,
    code: str,
) -> None:
    with pytest.raises(OrderAuditVerificationError) as error:
        RuntimeOrderAuditService._validate_economics(
            role=role,
            requested=Decimal(requested),
            executed=Decimal(executed),
            average=Decimal(average) if average is not None else None,
            status=status,
            actual_order_id=actual_order_id,
        )
    assert error.value.code == code


def test_runtime_status_transition_matrix_and_factory_guards() -> None:
    assert RuntimeOrderAuditService.from_protective_service(None) is None
    assert RuntimeOrderAuditService.from_protective_service(object()) is None  # type: ignore[arg-type]
    assert RuntimeOrderAuditService._status_transition_allowed("NEW", "NEW")
    assert RuntimeOrderAuditService._status_transition_allowed("CANCELED", "CANCELLED")
    assert RuntimeOrderAuditService._status_transition_allowed("NEW", "PARTIALLY_FILLED")
    assert RuntimeOrderAuditService._status_transition_allowed("PARTIALLY_FILLED", "FILLED")
    assert not RuntimeOrderAuditService._status_transition_allowed("FILLED", "NEW")
    assert not RuntimeOrderAuditService._status_transition_allowed("UNKNOWN", "NEW")


def test_symbol_scoped_fill_rejects_missing_order_and_conflict(tmp_path: Path) -> None:
    repositories = _repositories(tmp_path)
    fill = _Fill(
        exchange_trade_id="7",
        quantity=Decimal("1"),
        price=Decimal("100"),
        filled_at=NOW,
    )
    with repositories.persistence.transaction() as session:
        with pytest.raises(OrderAuditVerificationError) as missing:
            RuntimeOrderAuditService._persist_fill(session, "missing", fill)
    assert missing.value.code == "ORDER_AUDIT_FILL_ORDER_MISSING"

    with repositories.persistence.transaction() as session:
        session.add(
            ExchangeOrderRow(
                order_id="entry:one",
                signal_id=None,
                trade_id=None,
                client_order_id="client-one",
                exchange_order_id="exchange-one",
                symbol="BTCUSDT",
                status="FILLED",
                quantity_text="1",
                average_price_text="100",
                payload_json="{}",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with repositories.persistence.transaction() as session:
        RuntimeOrderAuditService._persist_fill(session, "entry:one", fill)
    conflicting = _Fill(
        exchange_trade_id="7",
        quantity=Decimal("2"),
        price=Decimal("100"),
        filled_at=NOW,
    )
    with repositories.persistence.transaction() as session:
        with pytest.raises(OrderAuditVerificationError) as conflict:
            RuntimeOrderAuditService._persist_fill(
                session,
                "entry:one",
                conflicting,
            )
    assert conflict.value.code == "ORDER_AUDIT_FILL_IDENTITY_CONFLICT"


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("unexpected", "ORDER_AUDIT_INVALID"),
        ("truncated", "ORDER_AUDIT_FILL_WINDOW_TRUNCATED"),
        ("manual-zero", "ORDER_AUDIT_MANUAL_REQUEST_INVALID"),
        ("regular-client", "ORDER_AUDIT_ORDER_IDENTITY_INVALID"),
        ("regular-order", "ORDER_AUDIT_ORDER_IDENTITY_INVALID"),
        ("algo-order", "ORDER_AUDIT_ALGO_IDENTITY_INVALID"),
        ("requested", "ORDER_AUDIT_REQUESTED_QUANTITY_MISMATCH"),
        ("side", "ORDER_AUDIT_ORDER_SIDE_INVALID"),
        ("status", "ORDER_AUDIT_STATUS_MISSING"),
        ("duplicate-fill", "ORDER_AUDIT_FILL_IDENTITY_INVALID"),
        ("average-mismatch", "ORDER_AUDIT_AVERAGE_PRICE_MISMATCH"),
        ("average-without-fill", "ORDER_AUDIT_AVERAGE_PRICE_WITHOUT_FILL"),
    ],
)
def test_reconcile_rejects_exchange_and_identity_edge_cases(
    tmp_path: Path,
    case: str,
    code: str,
) -> None:
    trade = _trade()
    client: StubOrderAuditClient
    if case == "unexpected":
        client = UnexpectedClient()
    elif case == "truncated":
        client = TruncatedClient()
    else:
        client = StubOrderAuditClient()

    if case == "manual-zero":
        trade = _trade(
            lifecycle=DemoTradeLifecycle.CLOSED,
            closed_reason=DemoTradeCloseReason.MANUAL_CLOSE,
            protective_exit_filled_quantity=Decimal("0.1"),
        )
    elif case == "regular-client":
        client.entry["clientOrderId"] = "wrong"
    elif case == "regular-order":
        client.entry["orderId"] = "wrong"
    elif case == "algo-order":
        client.stop["orderId"] = "wrong"
    elif case == "requested":
        client.entry["origQty"] = "0.2"
    elif case == "side":
        client.entry["side"] = "SELL"
    elif case == "status":
        client.entry.pop("status")
    elif case == "duplicate-fill":
        client.fills.append(dict(client.fills[0]))
    elif case == "average-mismatch":
        client.entry["avgPrice"] = "101"
    elif case == "average-without-fill":
        client.entry["executedQty"] = "0"
        client.entry["avgPrice"] = "100"
        client.fills = []

    service, _, _, gate = _service(tmp_path, trade, client)
    report = service.reconcile()

    assert report.state is OrderAuditState.BLOCKED
    assert {item.code for item in report.findings} == {code}
    assert gate.snapshot().automation_ready is False


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("malformed", "ORDER_AUDIT_PAYLOAD_INVALID"),
        ("non-dict", "ORDER_AUDIT_PAYLOAD_INVALID"),
        ("legacy", "ORDER_AUDIT_LEGACY_ROW"),
        ("role", "ORDER_AUDIT_ROLE_INVALID"),
        ("client", "ORDER_AUDIT_TEXT_FIELD_INVALID"),
        ("parity", "ORDER_AUDIT_ROW_PARITY_INVALID"),
        ("time", "ORDER_AUDIT_TIMESTAMP_INVALID"),
        ("naive-time", "ORDER_AUDIT_TIMESTAMP_INVALID"),
        ("fill-list", "ORDER_AUDIT_FILL_LIST_INVALID"),
        ("duplicate-list", "ORDER_AUDIT_FILL_LIST_INVALID"),
    ],
)
def test_records_reject_malformed_or_conflicting_durable_rows(
    tmp_path: Path,
    case: str,
    code: str,
) -> None:
    service = RuntimeOrderAuditService(
        StubTradeSource(_trade()),
        StubOrderAuditClient(),
        _repositories(tmp_path),
        _gate(),
        now_provider=lambda: NOW,
    )
    assert service.reconcile().state is OrderAuditState.READY
    repositories = service._repositories
    assert repositories is not None
    with repositories.persistence.transaction() as session:
        row = session.get(ExchangeOrderRow, "entry:entry-1")
        assert row is not None
        if case == "malformed":
            row.payload_json = "{bad"
        elif case == "non-dict":
            row.payload_json = "[]"
        else:
            payload = json.loads(row.payload_json)
            if case == "legacy":
                payload.pop("schema_version")
            elif case == "role":
                payload["role"] = "INVALID"
            elif case == "client":
                payload["client_order_id"] = ""
            elif case == "parity":
                row.status = "NEW"
            elif case == "time":
                payload["verified_at"] = "not-a-time"
            elif case == "naive-time":
                payload["verified_at"] = "2026-07-19T18:00:00"
            elif case == "fill-list":
                payload["exchange_trade_ids"] = "not-a-list"
            elif case == "duplicate-list":
                payload["exchange_trade_ids"] = ["1", "1"]
            row.payload_json = json.dumps(payload)

    records = service.records()

    assert records.state is OrderAuditState.BLOCKED
    assert records.blocking is True
    assert {item.code for item in records.findings} == {code}


def test_records_without_repository_are_unavailable() -> None:
    service = OrderAuditService(
        StubTradeSource(_trade()),
        StubOrderAuditClient(),
        None,
        _gate(),
    )

    records = service.records()

    assert records.state is OrderAuditState.UNAVAILABLE
    assert records.blocking is True
    assert records.findings[0].code == "ORDER_AUDIT_DURABILITY_UNAVAILABLE"


def test_progression_guards_reject_durable_truth_conflicts(tmp_path: Path) -> None:
    client = StubOrderAuditClient()
    client.stop.update(
        {
            "status": "PARTIALLY_FILLED",
            "actualOrderId": "stop-actual-1",
            "executedQty": "0.04",
            "avgPrice": "99",
        }
    )
    client.fills.append(_fill("stop-actual-1", "stop-fill-1", "0.04", "99", "SELL"))
    service = RuntimeOrderAuditService(
        StubTradeSource(_trade()),
        client,
        _repositories(tmp_path),
        _gate(),
        now_provider=lambda: NOW,
    )
    assert service.reconcile().state is OrderAuditState.READY
    repositories = service._repositories
    assert repositories is not None
    with repositories.persistence.transaction() as session:
        row = session.get(ExchangeOrderRow, "stop:stop-algo-1")
        assert row is not None
        fill = _Fill(
            exchange_trade_id="stop-fill-1",
            quantity=Decimal("0.04"),
            price=Decimal("99"),
            filled_at=NOW,
        )
        base = _Evidence(
            role=OrderAuditRole.STOP_LOSS,
            source="verified",
            client_order_id="af-stop-1",
            exchange_order_id="stop-algo-1",
            actual_order_id="stop-actual-1",
            requested_quantity=Decimal("0.1"),
            executed_quantity=Decimal("0.04"),
            average_fill_price=Decimal("99"),
            status="PARTIALLY_FILLED",
            fills=(fill,),
        )
        cases = [
            (
                base.__class__(**{**base.__dict__, "requested_quantity": Decimal("0.2")}),
                "ORDER_AUDIT_IMMUTABLE_FIELD_CHANGED",
            ),
            (
                base.__class__(**{**base.__dict__, "actual_order_id": "stop-actual-2"}),
                "ORDER_AUDIT_ACTUAL_ORDER_ID_CHANGED",
            ),
            (
                base.__class__(
                    **{
                        **base.__dict__,
                        "executed_quantity": Decimal("0.03"),
                    }
                ),
                "ORDER_AUDIT_EXECUTED_QUANTITY_REGRESSION",
            ),
            (
                base.__class__(
                    **{
                        **base.__dict__,
                        "average_fill_price": Decimal("98"),
                    }
                ),
                "ORDER_AUDIT_AVERAGE_PRICE_CONFLICT",
            ),
            (
                base.__class__(**{**base.__dict__, "status": "NEW"}),
                "ORDER_AUDIT_STATUS_REGRESSION",
            ),
            (
                base.__class__(**{**base.__dict__, "fills": ()}),
                "ORDER_AUDIT_FILL_EVIDENCE_REGRESSION",
            ),
        ]
        for evidence, code in cases:
            with pytest.raises(OrderAuditVerificationError) as error:
                service._validate_progression(row, evidence)
            assert error.value.code == code


def test_parser_and_fill_helpers_reject_invalid_values() -> None:
    service = OrderAuditService
    assert service._text(None) is None
    assert service._text("  ") is None
    assert service._optional_non_negative_decimal(None) is None
    assert service._optional_positive_decimal("0") is None
    assert service._decimal_text(None) is None
    assert service._requested_quantity({}, Decimal("1")) == Decimal("1")
    assert service._status({"algoStatus": "NEW"}) == "NEW"
    assert service._aware(datetime(2026, 7, 19, 18, 0)).tzinfo is UTC
    assert service._aware(NOW) == NOW

    invalid_calls = [
        lambda: service._required_text(""),
        lambda: service._positive_decimal("0"),
        lambda: service._non_negative_decimal("-1"),
        lambda: service._finite_decimal("bad"),
        lambda: service._finite_decimal("NaN"),
        lambda: service._string_list("bad"),
        lambda: service._string_list(["1", "1"]),
        lambda: service._timestamp_ms("bad"),
        lambda: service._timestamp_ms(0),
        lambda: service._timestamp_text("bad"),
        lambda: service._timestamp_text("2026-07-19T18:00:00"),
        lambda: service._requested_quantity({"origQty": "2"}, Decimal("1")),
        lambda: service._status({}),
        lambda: service._validate_side({"side": "SELL"}, "BUY"),
    ]
    for call in invalid_calls:
        with pytest.raises(OrderAuditVerificationError):
            call()

    fill = _Fill(
        exchange_trade_id="1",
        quantity=Decimal("1"),
        price=Decimal("100"),
        filled_at=NOW,
    )
    with pytest.raises(OrderAuditVerificationError) as mismatch:
        service._execution_economics(
            {"executedQty": "1", "avgPrice": "101"},
            [fill],
        )
    assert mismatch.value.code == "ORDER_AUDIT_AVERAGE_PRICE_MISMATCH"
    with pytest.raises(OrderAuditVerificationError) as no_fill:
        service._execution_economics(
            {"executedQty": "0", "avgPrice": "100"},
            [],
        )
    assert no_fill.value.code == "ORDER_AUDIT_AVERAGE_PRICE_WITHOUT_FILL"

    duplicate = {
        "symbol": "BTCUSDT",
        "orderId": "1",
        "id": "1",
        "side": "BUY",
        "qty": "1",
        "price": "100",
        "time": FILL_TIME,
    }
    with pytest.raises(OrderAuditVerificationError) as duplicate_error:
        service._fills_for_order(
            [duplicate, dict(duplicate)],
            symbol="BTCUSDT",
            order_id="1",
            expected_side="BUY",
        )
    assert duplicate_error.value.code == "ORDER_AUDIT_FILL_IDENTITY_INVALID"


def test_route_fallback_returns_ready_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories = _repositories(tmp_path)
    source = StubTradeSource(_trade())
    client = StubOrderAuditClient()
    gate = _gate()
    monkeypatch.setattr(order_audit_route, "get_execution_service", lambda: source)
    monkeypatch.setattr(order_audit_route, "get_private_demo_client", lambda: client)
    monkeypatch.setattr(order_audit_route, "get_recovery_gate", lambda: gate)
    app = FastAPI()
    app.state.trading_state_repositories = repositories
    app.include_router(order_audit_route.router, prefix="/api/v1")

    with TestClient(app) as test_client:
        response = test_client.get("/api/v1/order-audit/orders")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "READY"
    assert body["blocking"] is False
    assert body["count"] == 3
