"""BE-13 fail-closed and recovery edge verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.persistence.models import ExecutionIntentRow, TradeRow
from app.schemas.execution import DemoTradeCloseReason, DemoTradeLifecycle
from app.schemas.protective_lifecycle import ProtectiveLifecycleState
from app.services.protective_lifecycle import (
    ProtectiveLifecycleVerificationError,
    ProtectiveLifecycleVerificationService,
)
from tests.unit.test_be_13_protective_lifecycle import (
    FILL_TIME,
    NOW,
    StubLifecycleClient,
    StubTradeSource,
    _fill,
    _gate,
    _repositories,
    _trade,
)


class InvalidPositionClient(StubLifecycleClient):
    def positions(self) -> list[dict[str, Any]]:
        return [{"positionAmt": "0.1"}]


class UnavailablePositionClient(StubLifecycleClient):
    def positions(self) -> list[dict[str, Any]]:
        raise BinanceDemoPrivateClientError("position source unavailable")


class EmptyIncomeClient(StubLifecycleClient):
    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return []


class RetryCancelClient(StubLifecycleClient):
    cancel_valid = False

    def cancel_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]:
        if self.cancel_valid:
            return super().cancel_order(
                symbol=symbol,
                orig_client_order_id=orig_client_order_id,
            )
        self.cancelled.append(orig_client_order_id)
        return {
            "symbol": symbol,
            "clientOrderId": "wrong-client",
            "orderId": "wrong-order",
            "status": "NEW",
        }


def _service(
    tmp_path: Path,
    client: StubLifecycleClient | None,
) -> tuple[
    ProtectiveLifecycleVerificationService,
    StubTradeSource,
]:
    trade = _trade()
    source = StubTradeSource(trade)
    service = ProtectiveLifecycleVerificationService(
        source,
        client,
        _repositories(tmp_path, trade),
        _gate(),
        now_provider=lambda: NOW,
    )
    return service, source


def test_missing_exchange_client_is_unavailable_and_published(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, None)

    report = service.reconcile()

    assert report.state is ProtectiveLifecycleState.UNAVAILABLE
    assert report.blocking is True
    assert report.findings[0].code == "PROTECTIVE_LIFECYCLE_DURABILITY_UNAVAILABLE"
    assert service.latest() == report


@pytest.mark.parametrize(
    ("client", "code"),
    [
        (InvalidPositionClient(position_quantity="0.1"), "PROTECTIVE_POSITION_PAYLOAD_INVALID"),
        (
            UnavailablePositionClient(position_quantity="0.1"),
            "PROTECTIVE_LIFECYCLE_POSITION_UNAVAILABLE",
        ),
    ],
)
def test_position_snapshot_failures_are_unavailable(
    tmp_path: Path,
    client: StubLifecycleClient,
    code: str,
) -> None:
    service, _ = _service(tmp_path, client)

    report = service.reconcile()

    assert report.state is ProtectiveLifecycleState.UNAVAILABLE
    assert report.findings[0].code == code


def test_fill_window_truncation_fails_closed(tmp_path: Path) -> None:
    client = StubLifecycleClient(
        position_quantity="0.1",
        fills=[
            {
                "symbol": "OTHER",
                "orderId": "other",
                "id": f"fill-{index}",
                "side": "SELL",
                "qty": "1",
                "price": "1",
                "time": FILL_TIME,
            }
            for index in range(1000)
        ],
    )
    service, _ = _service(tmp_path, client)

    report = service.reconcile()

    assert {item.code for item in report.findings} == {"PROTECTIVE_FILL_WINDOW_TRUNCATED"}


@pytest.mark.parametrize(
    ("position", "status", "executed", "quantity", "code"),
    [
        (None, "FILLED", "0.2", "0.2", "PROTECTIVE_EXIT_EXCEEDS_ENTRY_QUANTITY"),
        ("0.06", "PARTIALLY_FILLED", "0.03", "0.04", "PROTECTIVE_ORDER_FILL_QUANTITY_MISMATCH"),
        ("0.06", "NEW", "0.04", "0.04", "PROTECTIVE_PARTIAL_STATUS_INVALID"),
        (None, "PARTIALLY_FILLED", "0.1", "0.1", "PROTECTIVE_TERMINAL_STATUS_INVALID"),
        (None, "PARTIALLY_FILLED", "0.04", "0.04", "PROTECTIVE_PARTIAL_POSITION_MISSING"),
        ("0.05", "PARTIALLY_FILLED", "0.04", "0.04", "PROTECTIVE_REMAINING_POSITION_MISMATCH"),
    ],
)
def test_protective_quantity_and_status_mismatches_fail_closed(
    tmp_path: Path,
    position: str | None,
    status: str,
    executed: str,
    quantity: str,
    code: str,
) -> None:
    client = StubLifecycleClient(
        position_quantity=position,
        stop_status=status,
        stop_executed=executed,
        stop_actual_order_id="stop-actual-1",
        fills=[_fill("stop-actual-1", "fill-stop", quantity, "99")],
    )
    service, source = _service(tmp_path, client)

    report = service.reconcile()

    assert {item.code for item in report.findings} == {code}
    assert source.store_calls == 0


@pytest.mark.parametrize(
    ("position", "code"),
    [
        (None, "UNVERIFIED_POSITION_CLOSE"),
        ("-0.1", "PROTECTIVE_POSITION_DIRECTION_MISMATCH"),
    ],
)
def test_unexplained_position_state_fails_closed(
    tmp_path: Path,
    position: str | None,
    code: str,
) -> None:
    service, _ = _service(
        tmp_path,
        StubLifecycleClient(position_quantity=position),
    )

    report = service.reconcile()

    assert {item.code for item in report.findings} == {code}


def test_full_close_with_missing_income_does_not_mutate_trade(tmp_path: Path) -> None:
    client = EmptyIncomeClient(
        position_quantity=None,
        take_status="FINISHED",
        take_executed="0.1",
        take_actual_order_id="take-actual-1",
        fills=[
            _fill("entry-1", "fill-entry", "0.1", "101"),
            _fill("take-actual-1", "fill-take", "0.1", "105"),
        ],
    )
    service, source = _service(tmp_path, client)

    report = service.reconcile()

    assert report.state is ProtectiveLifecycleState.BLOCKED
    assert report.findings[0].code.startswith("PROTECTIVE_CLOSE_JOURNAL_")
    assert source.trade.lifecycle is DemoTradeLifecycle.OPEN
    assert source.store_calls == 0


def test_sibling_cancel_failure_is_durable_and_retryable(tmp_path: Path) -> None:
    trade = _trade()
    source = StubTradeSource(trade)
    repositories = _repositories(tmp_path, trade)
    client = RetryCancelClient(
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

    first = service.reconcile()
    client.cancel_valid = True
    second = service.reconcile()

    assert first.state is ProtectiveLifecycleState.BLOCKED
    assert first.findings[0].code == "PROTECTIVE_SIBLING_CANCEL_UNVERIFIED"
    assert source.trade.lifecycle is DemoTradeLifecycle.CLOSED
    assert second.state is ProtectiveLifecycleState.IN_SYNC
    assert source.trade.protective_sibling_cancelled is True
    assert client.cancelled == ["af-stop-1", "af-stop-1"]


def test_missing_durable_trade_rejects_verified_partial_event(tmp_path: Path) -> None:
    trade = _trade()
    repositories = _repositories(tmp_path, trade)
    with repositories.persistence.transaction() as session:
        row = session.get(TradeRow, trade.trade_id)
        assert row is not None
        session.delete(row)
    source = StubTradeSource(trade)
    service = ProtectiveLifecycleVerificationService(
        source,
        StubLifecycleClient(
            position_quantity="0.06",
            stop_status="PARTIALLY_FILLED",
            stop_executed="0.04",
            stop_actual_order_id="stop-actual-1",
            fills=[_fill("stop-actual-1", "fill-stop", "0.04", "99")],
        ),
        repositories,
        _gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()

    assert {item.code for item in report.findings} == {"PROTECTIVE_LIFECYCLE_TRADE_NOT_DURABLE"}


def test_malformed_existing_event_payload_fails_closed(tmp_path: Path) -> None:
    trade = _trade()
    repositories = _repositories(tmp_path, trade)
    with repositories.persistence.transaction() as session:
        session.add(
            ExecutionIntentRow(
                intent_id="event-bad",
                operation="PROTECTIVE_LIFECYCLE",
                subject_id="event-bad",
                signal_id=trade.signal_id,
                state="PARTIAL_CLOSE",
                client_order_ids_json='["af-stop-1"]',
                payload_json="{not-json",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    source = StubTradeSource(trade)
    service = ProtectiveLifecycleVerificationService(
        source,
        StubLifecycleClient(
            position_quantity="0.06",
            stop_status="PARTIALLY_FILLED",
            stop_executed="0.04",
            stop_actual_order_id="stop-actual-1",
            fills=[_fill("stop-actual-1", "fill-stop", "0.04", "99")],
        ),
        repositories,
        _gate(),
        now_provider=lambda: NOW,
    )

    report = service.reconcile()

    assert {item.code for item in report.findings} == {"PROTECTIVE_EVENT_PAYLOAD_INVALID"}


def test_parser_helpers_reject_invalid_exchange_values() -> None:
    service = ProtectiveLifecycleVerificationService

    assert service._text(None) is None
    assert service._text("  ") is None
    assert service._optional_decimal(None) is None
    with pytest.raises(ProtectiveLifecycleVerificationError):
        service._positive_decimal("0")
    with pytest.raises(ProtectiveLifecycleVerificationError):
        service._optional_decimal("-1")
    with pytest.raises(ProtectiveLifecycleVerificationError):
        service._finite_decimal("not-a-decimal")
    with pytest.raises(ProtectiveLifecycleVerificationError):
        service._finite_decimal("NaN")
    with pytest.raises(ProtectiveLifecycleVerificationError):
        service._timestamp("bad")
    with pytest.raises(ProtectiveLifecycleVerificationError):
        service._timestamp(0)


def test_sibling_identity_requires_verified_close_reason() -> None:
    trade = _trade()
    stop_closed = trade.model_copy(
        update={
            "lifecycle": DemoTradeLifecycle.CLOSED,
            "closed_reason": DemoTradeCloseReason.STOP_LOSS,
            "closed_at": NOW,
        }
    )
    take_closed = stop_closed.model_copy(update={"closed_reason": DemoTradeCloseReason.TAKE_PROFIT})

    assert ProtectiveLifecycleVerificationService._sibling_identity(stop_closed) == (
        "af-take-1",
        "take-algo-1",
    )
    assert ProtectiveLifecycleVerificationService._sibling_identity(take_closed) == (
        "af-stop-1",
        "stop-algo-1",
    )
    with pytest.raises(ProtectiveLifecycleVerificationError):
        ProtectiveLifecycleVerificationService._sibling_identity(trade)
