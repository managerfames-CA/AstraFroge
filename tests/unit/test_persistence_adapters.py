"""Behavioral coverage for persistence-backed runtime service adapters."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.persistence.database import Persistence
from app.persistence.models import (
    ExchangeOrderRow,
    FillRow,
    PositionRow,
    RiskDecisionRow,
    SignalLifecycleRow,
    SignalRow,
    TradeRow,
)
from app.persistence.repositories import TradingStateRepositories
from app.persistence.service_adapters import (
    PersistentExecutionService,
    PersistentRiskService,
    PersistentSignalService,
)
from app.schemas.execution import DemoTradeLifecycle
from app.schemas.signals import SignalLifecycle
from app.schemas.trade_management import TradeCloseRequest
from app.services.trade_management import TradeManagementService
from tests.unit.test_execution import SIGNAL_ID, StubDemoClient, StubRisk, _enabled_settings
from tests.unit.test_real_risk_engine import (
    StubPrivateClient,
    StubSignals,
    _settings,
    _signal,
)
from tests.unit.test_signals import StubScanner
from tests.unit.test_trade_management import StubCloseClient


@pytest.fixture
def repositories(tmp_path: Path) -> Iterator[TradingStateRepositories]:
    persistence = Persistence(f"sqlite+pysqlite:///{tmp_path / 'adapter.db'}")
    persistence.initialize()
    repository = TradingStateRepositories(persistence)
    try:
        yield repository
    finally:
        persistence.close()


def test_signal_adapter_writes_history_and_recovers(
    repositories: TradingStateRepositories,
) -> None:
    first = PersistentSignalService(StubScanner(), repositories)  # type: ignore[arg-type]
    projected = first.signals()
    active = projected.signals[0]
    blocked = first.mark_risk_blocked(
        active.signal_id,
        reason="RISK_POLICY_BLOCK",
        changed_at=active.updated_at,
    )

    assert blocked is not None
    assert blocked.lifecycle is SignalLifecycle.RISK_BLOCKED
    assert first.status().active_signal_count == 0

    recovered = PersistentSignalService(StubScanner(), repositories)  # type: ignore[arg-type]
    record = recovered.get(active.signal_id)
    assert record is not None
    assert record.lifecycle is SignalLifecycle.RISK_BLOCKED

    with repositories.persistence.transaction() as session:
        signal_count = session.scalar(select(func.count()).select_from(SignalRow))
        history_count = session.scalar(
            select(func.count()).select_from(SignalLifecycleRow)
        )
    assert signal_count == 3
    assert history_count >= 4


def test_risk_adapter_persists_generated_assessments(
    repositories: TradingStateRepositories,
) -> None:
    service = PersistentRiskService(
        StubSignals([_signal()]),  # type: ignore[arg-type]
        _settings(),
        StubPrivateClient(),
        repositories,
    )

    assessments = service.assessments()
    status = service.status()

    assert assessments.count == 1
    assert status.account_snapshot_available is True
    with repositories.persistence.transaction() as session:
        rows = list(session.scalars(select(RiskDecisionRow)))
    assert rows
    assert rows[0].signal_id == assessments.assessments[0].signal_id
    assert "RISK_APPROVED" in rows[0].audit_codes_json


def test_execution_adapter_persists_verified_orders_fill_and_trade(
    repositories: TradingStateRepositories,
) -> None:
    service = PersistentExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        StubDemoClient(),
        repositories,
    )

    trade = service.activate(SIGNAL_ID)

    assert trade.lifecycle is DemoTradeLifecycle.OPEN
    with repositories.persistence.transaction() as session:
        orders = list(session.scalars(select(ExchangeOrderRow)))
        fills = list(session.scalars(select(FillRow)))
        stored_trade = session.get(TradeRow, trade.trade_id)
    assert len(orders) == 3
    assert len(fills) == 1
    assert fills[0].quantity_text == "0.107"
    assert stored_trade is not None
    assert stored_trade.lifecycle == "OPEN"

    recovered = PersistentExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        StubDemoClient(),
        repositories,
    )
    assert recovered.trades().trades[0].trade_id == trade.trade_id


def test_execution_account_persists_position_snapshot(
    repositories: TradingStateRepositories,
) -> None:
    class PositionClient(StubDemoClient):
        def account(self) -> dict[str, object]:
            return {
                "canTrade": True,
                "totalWalletBalance": "100",
                "availableBalance": "90",
                "totalUnrealizedProfit": "2.5",
                "assets": [
                    {
                        "asset": "USDT",
                        "walletBalance": "100",
                        "availableBalance": "90",
                        "unrealizedProfit": "2.5",
                    }
                ],
            }

        def positions(self) -> list[dict[str, object]]:
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.0100",
                    "entryPrice": "65000.1200",
                    "unRealizedProfit": "2.5",
                }
            ]

    service = PersistentExecutionService(
        StubRisk(),  # type: ignore[arg-type]
        _enabled_settings(),
        PositionClient(),
        repositories,
    )

    first = service.account()
    second = service.account()

    assert first.open_positions[0].quantity == Decimal("0.0100")
    assert second.open_positions[0].entry_price == Decimal("65000.1200")
    with repositories.persistence.transaction() as session:
        positions = list(session.scalars(select(PositionRow)))
    assert len(positions) == 1
    assert positions[0].quantity_text == "0.0100"


def test_trade_management_close_updates_durable_trade(
    repositories: TradingStateRepositories,
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
        income_payload=[
            {"symbol": opened.symbol, "incomeType": "REALIZED_PNL", "income": "0.535"}
        ],
    )
    management = TradeManagementService(execution, close_client)

    closed = management.close_trade(opened.trade_id, TradeCloseRequest())

    assert closed.lifecycle is DemoTradeLifecycle.CLOSED
    with repositories.persistence.transaction() as session:
        row = session.get(TradeRow, opened.trade_id)
    assert row is not None
    assert row.lifecycle == "CLOSED"
    assert row.closed_at is not None
