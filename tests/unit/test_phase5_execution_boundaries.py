"""Focused Phase 5 write-ownership and query-before-retry tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.errors import AppError
from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.services.execution_facade import WorkerIsolatedExecutionService
from app.services.execution_private_adapter import (
    QueryBeforeRetrySnapshotPrivateClient,
)


class _Snapshots:
    def invalidate(self) -> None:
        return None


class _RawPrivateClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.query_calls = 0
        self.place_calls = 0

    def query_algo_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]:
        self.query_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]

    def place_protective_order(self, **kwargs: Any) -> dict[str, Any]:
        self.place_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


def _not_found() -> BinanceDemoPrivateClientError:
    return BinanceDemoPrivateClientError(
        "Order does not exist",
        status_code=400,
        exchange_code=-2013,
    )


def _submit(adapter: QueryBeforeRetrySnapshotPrivateClient) -> dict[str, Any]:
    return adapter.place_protective_order(
        symbol="BTCUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        quantity="0.1",
        stop_price="95",
        new_client_order_id="af-s-stable",
    )


def test_existing_protection_is_reused_before_submission() -> None:
    existing = {
        "orderId": 123,
        "clientOrderId": "af-s-stable",
        "status": "NEW",
    }
    raw = _RawPrivateClient([existing])
    adapter = QueryBeforeRetrySnapshotPrivateClient(  # type: ignore[arg-type]
        raw,
        _Snapshots(),  # type: ignore[arg-type]
    )

    assert _submit(adapter) == existing
    assert raw.query_calls == 1
    assert raw.place_calls == 0


def test_missing_protection_is_submitted_once() -> None:
    placed = {
        "orderId": 456,
        "clientOrderId": "af-s-stable",
        "status": "NEW",
    }
    raw = _RawPrivateClient([_not_found(), placed])
    adapter = QueryBeforeRetrySnapshotPrivateClient(  # type: ignore[arg-type]
        raw,
        _Snapshots(),  # type: ignore[arg-type]
    )

    assert _submit(adapter) == placed
    assert raw.query_calls == 1
    assert raw.place_calls == 1


def test_lost_protection_response_is_recovered_by_identity_query() -> None:
    recovered = {
        "orderId": 789,
        "clientOrderId": "af-s-stable",
        "status": "NEW",
    }
    submission_error = BinanceDemoPrivateClientError("Connection lost after submit")
    raw = _RawPrivateClient([_not_found(), submission_error, recovered])
    adapter = QueryBeforeRetrySnapshotPrivateClient(  # type: ignore[arg-type]
        raw,
        _Snapshots(),  # type: ignore[arg-type]
    )

    assert _submit(adapter) == recovered
    assert raw.query_calls == 2
    assert raw.place_calls == 1


class _InnerExecution:
    def __init__(self) -> None:
        self.activate_calls = 0

    def activate(self, signal_id: str, request: object | None = None) -> object:
        self.activate_calls += 1
        return object()


class _Commands:
    def __init__(self) -> None:
        self.enqueue_all_calls = 0

    def enqueue_all_ready(self) -> int:
        self.enqueue_all_calls += 1
        return 2


def test_compatibility_facade_cannot_submit_new_entry() -> None:
    inner = _InnerExecution()
    commands = _Commands()
    facade = WorkerIsolatedExecutionService(  # type: ignore[arg-type]
        inner,
        commands,  # type: ignore[arg-type]
    )

    with pytest.raises(AppError) as exc:
        facade.activate("a" * 64)

    assert exc.value.code == "DIRECT_EXECUTION_FORBIDDEN"
    assert inner.activate_calls == 0
    assert facade.auto_execute_pending() == 2
    assert commands.enqueue_all_calls == 1
    assert inner.activate_calls == 0
