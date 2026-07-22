"""Durability tests for execution and close intents."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.persistence.database import Persistence
from app.persistence.models import ExecutionIntentRow
from app.persistence.repositories import TradingStateRepositories
from app.persistence.service_adapters import PersistentExecutionService
from app.schemas.trade_management import TradeCloseRequest
from app.services.trade_management import TradeManagementService
from tests.unit.test_execution import SIGNAL_ID, StubDemoClient, StubRisk, _enabled_settings
from tests.unit.test_trade_management import StubCloseClient


@pytest.fixture
def repositories(tmp_path: Path) -> Iterator[TradingStateRepositories]:
    persistence = Persistence(f"sqlite+pysqlite:///{tmp_path / 'intent.db'}")
    persistence.initialize()
    repository = TradingStateRepositories(persistence)
    try:
        yield repository
    finally:
        persistence.close()


def _intent(
    repositories: TradingStateRepositories,
    operation: str,
    subject_id: str,
) -> ExecutionIntentRow:
    intent_id = PersistentExecutionService._intent_id(operation, subject_id)
    with repositories.persistence.transaction() as session:
        row = session.get(ExecutionIntentRow, intent_id)
    assert row is not None
    return row


def test_pre_execution_persistence_failure_prevents_exchange_call(
    repositories: TradingStateRepositories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StubDemoClient()
    service = PersistentExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        client,
        repositories,
    )

    def fail_intent(**_: object) -> ExecutionIntentRow:
        raise RuntimeError("intent database unavailable")

    monkeypatch.setattr(service, "_prepare_intent", fail_intent)

    with pytest.raises(RuntimeError, match="intent database unavailable"):
        service.activate(SIGNAL_ID)

    assert client.market_orders == []
    assert client.protective_orders == []


def test_exchange_success_final_db_failure_leaves_recovery_intent(
    repositories: TradingStateRepositories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StubDemoClient()
    service = PersistentExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        client,
        repositories,
    )

    def fail_final(_: object) -> None:
        raise RuntimeError("final persistence failed")

    monkeypatch.setattr(service, "_persist_open_result", fail_final)

    with pytest.raises(RuntimeError, match="final persistence failed"):
        service.activate(SIGNAL_ID)

    assert len(client.market_orders) == 1
    intent = _intent(repositories, "OPEN", SIGNAL_ID)
    assert intent.state == "RECOVERY_REQUIRED"
    assert "FINAL_PERSISTENCE_FAILED" in intent.payload_json


class IdempotentDemoClient(StubDemoClient):
    """Exchange stub that resolves deterministic client IDs on retry."""

    def __init__(self) -> None:
        super().__init__()
        self.orders: dict[str, dict[str, Any]] = {}

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        existing = self.orders.get(orig_client_order_id)
        if existing is not None:
            return existing
        raise BinanceDemoPrivateClientError(
            "Order does not exist",
            status_code=400,
            exchange_code=-2013,
        )

    def place_market_order(self, **kwargs: Any) -> dict[str, Any]:
        payload = super().place_market_order(**kwargs)
        self.orders[str(payload["clientOrderId"])] = payload
        return payload

    def place_protective_order(self, **kwargs: Any) -> dict[str, Any]:
        payload = super().place_protective_order(**kwargs)
        self.orders[str(payload["clientOrderId"])] = payload
        return payload


def test_retry_uses_same_ids_without_duplicate_exchange_order(
    repositories: TradingStateRepositories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = IdempotentDemoClient()
    first = PersistentExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        client,
        repositories,
    )

    def fail_final(_: object) -> None:
        raise RuntimeError("final persistence failed")

    monkeypatch.setattr(first, "_persist_open_result", fail_final)
    with pytest.raises(RuntimeError):
        first.activate(SIGNAL_ID)

    market_count = len(client.market_orders)
    protection_count = len(client.protective_orders)

    second = PersistentExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        client,
        repositories,
    )
    trade = second.activate(SIGNAL_ID)

    assert trade.signal_id == SIGNAL_ID
    assert len(client.market_orders) == market_count
    assert len(client.protective_orders) == protection_count
    assert _intent(repositories, "OPEN", SIGNAL_ID).state == "PROTECTED"


def test_close_final_persistence_failure_leaves_recovery_intent(
    repositories: TradingStateRepositories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = PersistentExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        StubDemoClient(),
        repositories,
    )
    opened = execution.activate(SIGNAL_ID)
    close_client = StubCloseClient(
        exit_price="105",
        close_quantity="0.107",
        income_payload=[{"symbol": opened.symbol, "incomeType": "REALIZED_PNL", "income": "0.535"}],
    )
    management = TradeManagementService(execution, close_client)

    def fail_store(_: object) -> object:
        raise RuntimeError("close persistence failed")

    monkeypatch.setattr(execution, "store_trade", fail_store)

    with pytest.raises(RuntimeError, match="close persistence failed"):
        management.close_trade(opened.trade_id, TradeCloseRequest())

    intent = _intent(repositories, "CLOSE", opened.trade_id)
    assert intent.state == "RECOVERY_REQUIRED"
    assert "CLOSE_FINALIZATION_FAILED" in intent.payload_json
