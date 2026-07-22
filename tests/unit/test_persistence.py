"""Focused BE-01 durable persistence regression tests."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select

from app.core.config import Settings
from app.main import create_app
from app.persistence.database import (
    Persistence,
    PersistenceConfigurationError,
    validate_database_url,
)
from app.persistence.models import (
    ExchangeOrderRow,
    FillRow,
    MutationReplayKeyRow,
    PositionRow,
    RiskDecisionRow,
    SignalRow,
    TradeRow,
)
from app.persistence.repositories import TradingStateRepositories
from app.persistence.service_adapters import (
    PersistentExecutionService,
    reject_sensitive_payload,
)
from app.schemas.execution import (
    DemoProtectionState,
    DemoTradeLifecycle,
    DemoTradeRecord,
)
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.services.risk import RiskService

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'durable.db'}"


@pytest.fixture
def repositories(database_url: str) -> Iterator[TradingStateRepositories]:
    persistence = Persistence(database_url)
    persistence.initialize()
    repository = TradingStateRepositories(persistence)
    try:
        yield repository
    finally:
        persistence.close()


def _save_signal(repository: TradingStateRepositories, signal_id: str = "s" * 64) -> None:
    assert repository.save_signal(
        signal_id=signal_id,
        lifecycle="ACTIVE",
        payload={"signal_id": signal_id, "entry": "123.4500"},
        created_at=NOW,
        updated_at=NOW,
    )


def _trade() -> DemoTradeRecord:
    return DemoTradeRecord(
        trade_id="trade-restart-1",
        signal_id="t" * 64,
        symbol="BTCUSDT",
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        lifecycle=DemoTradeLifecycle.OPEN,
        protection_state=DemoProtectionState.PROTECTED,
        grade=ScannerGrade.A_PLUS,
        entry_price=Decimal("65000.123400"),
        stop_loss_price=Decimal("64000"),
        take_profit_price=Decimal("67000"),
        exchange_order_id="991",
        client_order_id="af-e-restart",
        stop_order_id="992",
        stop_client_order_id="af-s-restart",
        take_profit_order_id="993",
        take_profit_client_order_id="af-t-restart",
        requested_quantity=Decimal("0.0100"),
        executed_quantity=Decimal("0.0100"),
        order_status="FILLED",
        tracked_margin_usdt=Decimal("65"),
        opened_at=NOW,
        updated_at=NOW,
    )


def test_application_restart_recovers_runtime_trade_service(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRAFORGE_DATABASE_URL", database_url)
    settings = Settings(_env_file=None, environment="test", scanner_auto_start=False)

    first_app = create_app(settings)
    with TestClient(first_app):
        repository = cast(
            TradingStateRepositories,
            first_app.state.trading_state_repositories,
        )
        service = PersistentExecutionService(
            cast(RiskService, object()),
            settings,
            None,
            repository,
        )
        service.store_trade(_trade())

    second_app = create_app(settings)
    with TestClient(second_app):
        repository = cast(
            TradingStateRepositories,
            second_app.state.trading_state_repositories,
        )
        recovered = PersistentExecutionService(
            cast(RiskService, object()),
            settings,
            None,
            repository,
        ).trades()

    assert recovered.count == 1
    assert recovered.trades[0].trade_id == "trade-restart-1"
    assert recovered.trades[0].entry_price == Decimal("65000.123400")


def test_duplicate_stable_ids_do_not_create_duplicates(
    repositories: TradingStateRepositories,
) -> None:
    _save_signal(repositories)
    assert (
        repositories.save_signal(
            signal_id="s" * 64,
            lifecycle="ACTIVE",
            payload={"different": True},
            created_at=NOW,
            updated_at=NOW,
        )
        is False
    )
    with repositories.persistence.transaction() as session:
        count = session.scalar(select(func.count()).select_from(SignalRow))
    assert count == 1


def test_mutation_replay_claims_persist_exact_hashes(
    repositories: TradingStateRepositories,
) -> None:
    claimed, row = repositories.claim_mutation_replay(
        key_hash="a" * 64,
        fingerprint="b" * 64,
        action="POST /api/v1/scanner/stop",
        now=NOW,
        expires_at=NOW,
        cache_limit=5,
    )

    assert claimed is True
    assert row is not None
    with repositories.persistence.transaction() as session:
        stored = session.get(MutationReplayKeyRow, "a" * 64)
    assert stored is not None
    assert stored.fingerprint == "b" * 64
    assert stored.action == "POST /api/v1/scanner/stop"


def test_decimal_values_retain_exact_precision(
    repositories: TradingStateRepositories,
) -> None:
    assert repositories.save_trade(
        trade_id="trade-precision",
        signal_id="p" * 64,
        lifecycle="OPEN",
        symbol="BTCUSDT",
        quantity=Decimal("0.000123450000"),
        entry_price=Decimal("67890.123456789000"),
        payload={"source": "verified-fill"},
        opened_at=NOW,
        updated_at=NOW,
    )
    trade = repositories.trade("trade-precision")
    assert trade is not None
    assert trade.quantity_text == "0.000123450000"
    assert trade.entry_price_text == "67890.123456789000"


def test_signal_lifecycle_and_risk_audit_persist(
    repositories: TradingStateRepositories,
) -> None:
    _save_signal(repositories)
    assert repositories.append_signal_lifecycle(
        event_id="signal-event-1",
        signal_id="s" * 64,
        version=1,
        lifecycle="ACTIVE",
        audit_code="SCANNER_QUALIFIED",
        payload={"lifecycle": "ACTIVE"},
        changed_at=NOW,
    )
    assert repositories.save_risk_decision(
        decision_id="risk-1",
        signal_id="s" * 64,
        decision="BLOCKED",
        audit_codes=["MAX_OPEN_TRADES_REACHED"],
        payload={"approved_for_execution": False},
        assessed_at=NOW,
    )
    assert repositories.signal_history("s" * 64)[0].audit_code == "SCANNER_QUALIFIED"
    with repositories.persistence.transaction() as session:
        risk = session.get(RiskDecisionRow, "risk-1")
    assert risk is not None
    assert "MAX_OPEN_TRADES_REACHED" in risk.audit_codes_json


def test_orders_and_fills_are_atomic_and_related(
    repositories: TradingStateRepositories,
) -> None:
    with repositories.persistence.transaction() as session:
        assert repositories.save_order(
            order_id="order-1",
            signal_id="o" * 64,
            client_order_id="af-e-order-1",
            exchange_order_id="991",
            symbol="BTCUSDT",
            status="FILLED",
            quantity=Decimal("0.010"),
            average_price=Decimal("65000.25"),
            payload={"status": "FILLED"},
            created_at=NOW,
            updated_at=NOW,
            session=session,
        )
        assert repositories.save_fill(
            fill_id="fill-1",
            order_id="order-1",
            exchange_trade_id="exchange-trade-1",
            quantity=Decimal("0.010"),
            price=Decimal("65000.25"),
            commission=Decimal("0.02600010"),
            payload={"maker": False},
            filled_at=NOW,
            session=session,
        )
    order = repositories.order_with_fills("order-1")
    assert order is not None
    assert order.fills[0].commission_text == "0.02600010"


def test_transaction_failure_leaves_no_partial_records(
    repositories: TradingStateRepositories,
) -> None:
    with pytest.raises(RuntimeError, match="forced failure"):
        with repositories.persistence.transaction() as session:
            repositories.save_order(
                order_id="order-rollback",
                client_order_id="af-e-rollback",
                symbol="SOLUSDT",
                status="FILLED",
                payload={},
                created_at=NOW,
                updated_at=NOW,
                session=session,
            )
            repositories.save_fill(
                fill_id="fill-rollback",
                order_id="order-rollback",
                quantity=Decimal("1.25"),
                price=Decimal("150.00"),
                payload={},
                filled_at=NOW,
                session=session,
            )
            raise RuntimeError("forced failure")
    with repositories.persistence.transaction() as session:
        assert session.get(ExchangeOrderRow, "order-rollback") is None
        assert session.get(FillRow, "fill-rollback") is None


def test_position_and_trade_records_persist(
    repositories: TradingStateRepositories,
) -> None:
    assert repositories.save_position(
        position_id="position-1",
        symbol="ETHUSDT",
        quantity=Decimal("-0.2500"),
        entry_price=Decimal("3500.10"),
        payload={"position_side": "BOTH"},
        captured_at=NOW,
        updated_at=NOW,
    )
    assert repositories.save_trade(
        trade_id="trade-1",
        signal_id="z" * 64,
        lifecycle="CLOSED",
        symbol="ETHUSDT",
        quantity=Decimal("0.2500"),
        entry_price=Decimal("3500.10"),
        exit_price=Decimal("3400.20"),
        realized_pnl=Decimal("24.975000"),
        payload={"close_reason": "TAKE_PROFIT"},
        opened_at=NOW,
        closed_at=NOW,
        updated_at=NOW,
    )
    with repositories.persistence.transaction() as session:
        position = session.get(PositionRow, "position-1")
        trade = session.get(TradeRow, "trade-1")
    assert position is not None and position.quantity_text == "-0.2500"
    assert trade is not None and trade.realized_pnl_text == "24.975000"


def test_sensitive_payload_keys_are_rejected() -> None:
    reject_sensitive_payload({"order": {"status": "FILLED"}})
    with pytest.raises(ValueError, match="Sensitive persistence key"):
        reject_sensitive_payload({"exchange": {"api_key": "must-not-store"}})
    with pytest.raises(ValueError, match="Sensitive persistence key"):
        reject_sensitive_payload({"headers": {"Authorization": "Bearer secret"}})


def test_invalid_production_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASTRAFORGE_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    production = Settings(
        _env_file=None,
        environment="production",
        cors_origins=["https://frontend.example.com"],
    )
    with pytest.raises(PersistenceConfigurationError, match="DATABASE_URL"):
        validate_database_url(production)
    with pytest.raises(PersistenceConfigurationError, match="SQLite"):
        validate_database_url(production, "sqlite+pysqlite:///unsafe.db")


def test_production_accepts_standard_database_url_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASTRAFORGE_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://render-user:secret@render-host/render-db")
    production = Settings(
        _env_file=None,
        environment="production",
        cors_origins=["https://frontend.example.com"],
    )

    assert (
        validate_database_url(production)
        == "postgresql+psycopg://render-user:secret@render-host/render-db"
    )


def test_astraforge_database_url_takes_precedence_over_standard_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRAFORGE_DATABASE_URL", "postgresql://app-user:secret@app-host/app-db")
    monkeypatch.setenv("DATABASE_URL", "postgresql://render-user:secret@render-host/render-db")
    production = Settings(
        _env_file=None,
        environment="production",
        cors_origins=["https://frontend.example.com"],
    )

    assert (
        validate_database_url(production) == "postgresql+psycopg://app-user:secret@app-host/app-db"
    )


def test_production_accepts_legacy_postgres_scheme_via_psycopg_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASTRAFORGE_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://render-user:secret@render-host/render-db")
    production = Settings(
        _env_file=None,
        environment="production",
        cors_origins=["https://frontend.example.com"],
    )

    assert (
        validate_database_url(production)
        == "postgresql+psycopg://render-user:secret@render-host/render-db"
    )


def test_test_isolation_is_deterministic(tmp_path: Path) -> None:
    first = Persistence(f"sqlite+pysqlite:///{tmp_path / 'first.db'}")
    second = Persistence(f"sqlite+pysqlite:///{tmp_path / 'second.db'}")
    first.initialize()
    second.initialize()
    _save_signal(TradingStateRepositories(first), "i" * 64)
    assert TradingStateRepositories(first).signal("i" * 64) is not None
    assert TradingStateRepositories(second).signal("i" * 64) is None
    first.close()
    second.close()


def test_payload_fixture_contains_no_secrets() -> None:
    payload: dict[str, Any] = {
        "exchange_order_id": "123",
        "status": "FILLED",
        "quantity": "0.1",
    }
    reject_sensitive_payload(payload)


def test_duplicate_order_insert_does_not_erase_prior_order_and_fill(
    repositories: TradingStateRepositories,
) -> None:
    with repositories.persistence.transaction() as session:
        assert repositories.save_order(
            order_id="order-primary",
            signal_id="o" * 64,
            client_order_id="af-e-dup",
            exchange_order_id="1001",
            symbol="BTCUSDT",
            status="FILLED",
            quantity=Decimal("0.010"),
            average_price=Decimal("65000.25"),
            payload={"status": "FILLED"},
            created_at=NOW,
            updated_at=NOW,
            session=session,
        )
        assert repositories.save_fill(
            fill_id="fill-primary",
            order_id="order-primary",
            quantity=Decimal("0.010"),
            price=Decimal("65000.25"),
            payload={"source": "aggregate"},
            filled_at=NOW,
            session=session,
        )
        assert (
            repositories.save_order(
                order_id="order-duplicate",
                signal_id="o" * 64,
                client_order_id="af-e-dup",
                exchange_order_id="1002",
                symbol="BTCUSDT",
                status="FILLED",
                quantity=Decimal("0.010"),
                average_price=Decimal("65000.25"),
                payload={"status": "FILLED"},
                created_at=NOW,
                updated_at=NOW,
                session=session,
            )
            is False
        )

    with repositories.persistence.transaction() as session:
        assert session.get(ExchangeOrderRow, "order-primary") is not None
        assert session.get(FillRow, "fill-primary") is not None
        assert session.get(ExchangeOrderRow, "order-duplicate") is None


def test_duplicate_lifecycle_insert_does_not_erase_signal(
    repositories: TradingStateRepositories,
) -> None:
    with repositories.persistence.transaction() as session:
        assert repositories.save_signal(
            signal_id="l" * 64,
            lifecycle="ACTIVE",
            payload={"signal_id": "l" * 64},
            created_at=NOW,
            updated_at=NOW,
            session=session,
        )
        assert repositories.append_signal_lifecycle(
            event_id="lifecycle-1",
            signal_id="l" * 64,
            version=1,
            lifecycle="ACTIVE",
            audit_code="QUALIFIED",
            payload={"version": 1},
            changed_at=NOW,
            session=session,
        )
        assert (
            repositories.append_signal_lifecycle(
                event_id="lifecycle-2",
                signal_id="l" * 64,
                version=1,
                lifecycle="ACTIVE",
                audit_code="QUALIFIED",
                payload={"version": 1},
                changed_at=NOW,
                session=session,
            )
            is False
        )

    signal = repositories.signal("l" * 64)
    history = repositories.signal_history("l" * 64)
    assert signal is not None
    assert len(history) == 1


def test_secondary_unique_conflict_preserves_prior_outer_transaction_writes(
    repositories: TradingStateRepositories,
) -> None:
    with repositories.persistence.transaction() as session:
        assert repositories.save_signal(
            signal_id="u" * 64,
            lifecycle="ACTIVE",
            payload={"signal_id": "u" * 64},
            created_at=NOW,
            updated_at=NOW,
            session=session,
        )
        assert repositories.save_order(
            order_id="order-before-conflict",
            signal_id="u" * 64,
            client_order_id="af-e-before",
            exchange_order_id="shared-exchange-id",
            symbol="ETHUSDT",
            status="FILLED",
            quantity=Decimal("0.2500"),
            average_price=Decimal("3500.10"),
            payload={"status": "FILLED"},
            created_at=NOW,
            updated_at=NOW,
            session=session,
        )
        assert (
            repositories.save_order(
                order_id="order-conflict",
                signal_id="u" * 64,
                client_order_id="af-e-after",
                exchange_order_id="shared-exchange-id",
                symbol="ETHUSDT",
                status="FILLED",
                quantity=Decimal("0.2500"),
                average_price=Decimal("3500.10"),
                payload={"status": "FILLED"},
                created_at=NOW,
                updated_at=NOW,
                session=session,
            )
            is False
        )
        assert repositories.save_position(
            position_id="position-after-conflict",
            symbol="ETHUSDT",
            quantity=Decimal("0.2500"),
            entry_price=Decimal("3500.10"),
            payload={"source": "post-conflict"},
            captured_at=NOW,
            updated_at=NOW,
            session=session,
        )

    with repositories.persistence.transaction() as session:
        assert session.get(SignalRow, "u" * 64) is not None
        assert session.get(ExchangeOrderRow, "order-before-conflict") is not None
        assert session.get(ExchangeOrderRow, "order-conflict") is None
        assert session.get(PositionRow, "position-after-conflict") is not None


def test_caller_owned_session_remains_usable_after_duplicate_handling(
    repositories: TradingStateRepositories,
) -> None:
    with repositories.persistence.transaction() as session:
        assert repositories.save_order(
            order_id="session-order-1",
            signal_id="s" * 64,
            client_order_id="af-e-session",
            exchange_order_id="session-exchange-1",
            symbol="SOLUSDT",
            status="FILLED",
            quantity=Decimal("1.0"),
            average_price=Decimal("150"),
            payload={"status": "FILLED"},
            created_at=NOW,
            updated_at=NOW,
            session=session,
        )
        assert (
            repositories.save_order(
                order_id="session-order-2",
                signal_id="s" * 64,
                client_order_id="af-e-session",
                exchange_order_id="session-exchange-2",
                symbol="SOLUSDT",
                status="FILLED",
                quantity=Decimal("1.0"),
                average_price=Decimal("150"),
                payload={"status": "FILLED"},
                created_at=NOW,
                updated_at=NOW,
                session=session,
            )
            is False
        )
        assert repositories.save_trade(
            trade_id="session-trade-1",
            signal_id="session-signal",
            lifecycle="OPEN",
            symbol="SOLUSDT",
            quantity=Decimal("1.0"),
            entry_price=Decimal("150"),
            payload={"source": "session-still-usable"},
            opened_at=NOW,
            updated_at=NOW,
            session=session,
        )

    assert repositories.trade("session-trade-1") is not None


def test_runtime_bundle_contains_alembic_config_and_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    shutil.copyfile("alembic.ini", runtime_dir / "alembic.ini")
    shutil.copytree("migrations", runtime_dir / "migrations")
    monkeypatch.chdir(runtime_dir)

    persistence = Persistence(f"sqlite+pysqlite:///{runtime_dir / 'bundle.db'}")
    try:
        persistence.initialize()
        inspector = inspect(persistence.engine)
        assert inspector.has_table("signals")
        assert inspector.has_table("execution_intents")
    finally:
        persistence.close()


def test_dockerfile_explicitly_copies_alembic_runtime_artifacts() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "COPY alembic.ini ./" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile


def test_signal_lifecycle_event_id_longer_than_64_chars_can_be_saved(
    repositories: TradingStateRepositories,
) -> None:
    # 1. Save a signal
    signal_id = "test-event-id-len-signal"
    assert repositories.save_signal(
        signal_id=signal_id,
        lifecycle="ACTIVE",
        payload={"signal_id": signal_id},
        created_at=NOW,
        updated_at=NOW,
    )

    # 2. Append a lifecycle history entry with an event_id of 100 characters
    long_event_id = "e" * 100
    assert repositories.append_signal_lifecycle(
        event_id=long_event_id,
        signal_id=signal_id,
        version=1,
        lifecycle="ACTIVE",
        audit_code="SCANNER_QUALIFIED",
        payload={"lifecycle": "ACTIVE"},
        changed_at=NOW,
    )

    # 3. Verify it was correctly saved and retrieved without truncation
    history = repositories.signal_history(signal_id)
    assert len(history) == 1
    assert history[0].event_id == long_event_id
    assert len(history[0].event_id) == 100
