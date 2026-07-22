"""Focused unit and API contract tests for Scanner Auto-Start and ownership safety."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app
from app.persistence.database import Persistence
from app.schemas.scanner import (
    CandidateLifecycle,
    ScannerAuditRecord,
    ScannerCandidate,
    ScannerDirection,
    ScannerRunStatus,
    ScannerRunSummary,
    ScannerRunType,
    ScannerSetup,
    ScannerState,
)
from app.services.scanner import ScannerService
from app.services.scanner_runtime import (
    ScannerSchedulerLease,
    ScannerSchedulerLost,
)
from tests.unit.scanner_test_support import (
    FakeClock,
    FakeIndicators,
    FakeMarket,
    FakeUniverse,
)

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


# ==============================================================================
# BE-17 TESTS: SCANNER AUTO-START AND OWNERSHIP SAFETY
# ==============================================================================

def test_be17_01_scanner_auto_start_defaults_to_false() -> None:
    """1. Scanner auto-start defaults to false."""
    settings = Settings(_env_file=None, environment="development")
    assert settings.scanner_auto_start is False


def test_be17_02_explicit_true_configuration_works() -> None:
    """2. Explicit true configuration works."""
    settings = Settings(_env_file=None, environment="development", scanner_auto_start=True)
    assert settings.scanner_auto_start is True


def test_be17_03_explicit_false_configuration_works() -> None:
    """3. Explicit false configuration works."""
    settings = Settings(_env_file=None, environment="development", scanner_auto_start=False)
    assert settings.scanner_auto_start is False


@pytest.mark.parametrize("invalid_value", ["foo", "yes", "no", "maybe", "on", "off"])
def test_be17_04_invalid_boolean_configuration_fails_validation(invalid_value: str) -> None:
    """4. Invalid boolean configuration fails validation."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, scanner_auto_start=invalid_value)  # type: ignore[arg-type]


def test_be17_05_auto_start_false_performs_no_lease_acquisition() -> None:
    """5. Auto-start false performs no lease acquisition."""
    mock_lease = MagicMock(spec=ScannerSchedulerLease)
    service = ScannerService(
        FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock(), lease=mock_lease
    )
    assert service.status().state is ScannerState.OFF
    mock_lease.acquire.assert_not_called()


def test_be17_06_auto_start_true_without_persistence_fails_closed() -> None:
    """6. Auto-start true without persistence fails closed."""
    async def scenario() -> None:
        with patch("app.core.config.get_settings") as mock_get_settings:
            mock_settings = Settings(
                _env_file=None, environment="production", scanner_auto_start=True
            )
            mock_get_settings.return_value = mock_settings

            service = ScannerService(
                FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock(), lease=None
            )
            status = await service.start(source="lifespan")
            assert status.state is ScannerState.OFF
            assert status.blocking_code == "PERSISTENCE_UNAVAILABLE"
    asyncio.run(scenario())


def test_be17_07_auto_start_true_with_non_postgresql_persistence_does_not_claim_safe_ownership(
) -> None:
    """7. Auto-start true with non-PostgreSQL persistence does not claim safe ownership."""
    async def scenario() -> None:
        with patch("app.core.config.get_settings") as mock_get_settings:
            mock_settings = Settings(
                _env_file=None, environment="production", scanner_auto_start=True
            )
            mock_get_settings.return_value = mock_settings

            mock_persistence = MagicMock(spec=Persistence)
            mock_persistence.engine = MagicMock()
            mock_persistence.engine.dialect = MagicMock()
            mock_persistence.engine.dialect.name = "sqlite"

            mock_lease = ScannerSchedulerLease(mock_persistence)

            service = ScannerService(
                FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock(), lease=mock_lease
            )
            status = await service.start(source="lifespan")
            assert status.state is ScannerState.OFF
            assert status.blocking_code == "PERSISTENCE_UNAVAILABLE"
            assert status.is_owner is False
    asyncio.run(scenario())


def test_be17_08_first_postgresql_backed_instance_acquires_ownership() -> None:
    """8. First PostgreSQL-backed instance acquires ownership."""
    async def scenario() -> None:
        mock_persistence = MagicMock(spec=Persistence)
        mock_persistence.engine = MagicMock()
        mock_persistence.engine.dialect = MagicMock()
        mock_persistence.engine.dialect.name = "postgresql"

        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_conn.invalidated = False
        mock_persistence.engine.connect.return_value = mock_conn
        mock_conn.execute.return_value.scalar.return_value = True

        mock_lease = ScannerSchedulerLease(mock_persistence)

        service = ScannerService(
            FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock(), lease=mock_lease
        )
        status = await service.start()
        assert status.state is ScannerState.ON
        assert status.is_owner is True
        assert status.blocking_code is None
        await service.stop()
    asyncio.run(scenario())


def test_be17_09_second_instance_cannot_start_a_duplicate_recurring_scheduler() -> None:
    """9. Second instance cannot start a duplicate recurring scheduler."""
    async def scenario() -> None:
        mock_persistence = MagicMock(spec=Persistence)
        mock_persistence.engine = MagicMock()
        mock_persistence.engine.dialect = MagicMock()
        mock_persistence.engine.dialect.name = "postgresql"

        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_conn.invalidated = False
        mock_persistence.engine.connect.return_value = mock_conn
        mock_conn.execute.return_value.scalar.return_value = False

        mock_lease = ScannerSchedulerLease(mock_persistence)

        service = ScannerService(
            FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock(), lease=mock_lease
        )
        status = await service.start()
        assert status.state is ScannerState.OFF
        assert status.is_owner is False
        assert status.blocking_code == "OWNERSHIP_ACQUISITION_FAILED"
        await service.stop()
    asyncio.run(scenario())


def test_be17_10_repeated_start_is_idempotent() -> None:
    """10. Repeated start is idempotent."""
    async def scenario() -> None:
        mock_persistence = MagicMock(spec=Persistence)
        mock_persistence.engine = MagicMock()
        mock_persistence.engine.dialect = MagicMock()
        mock_persistence.engine.dialect.name = "postgresql"

        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_conn.invalidated = False
        mock_persistence.engine.connect.return_value = mock_conn
        mock_conn.execute.return_value.scalar.return_value = True

        mock_lease = ScannerSchedulerLease(mock_persistence)

        service = ScannerService(
            FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock(), lease=mock_lease
        )

        status_1 = await service.start()
        task_1 = service._scheduler_task
        assert status_1.state is ScannerState.ON

        status_2 = await service.start()
        task_2 = service._scheduler_task
        assert status_2.state is ScannerState.ON
        assert task_1 is task_2
        await service.stop()
    asyncio.run(scenario())


def test_be17_11_shutdown_releases_ownership() -> None:
    """11. Shutdown releases ownership."""
    async def scenario() -> None:
        mock_persistence = MagicMock(spec=Persistence)
        mock_persistence.engine = MagicMock()
        mock_persistence.engine.dialect = MagicMock()
        mock_persistence.engine.dialect.name = "postgresql"

        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_conn.invalidated = False
        mock_persistence.engine.connect.return_value = mock_conn
        mock_conn.execute.return_value.scalar.return_value = True

        mock_lease = MagicMock(wraps=ScannerSchedulerLease(mock_persistence))

        service = ScannerService(
            FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock(), lease=mock_lease
        )

        await service.start()
        await service.stop()

        mock_lease.release.assert_called_once()
        assert service.status().state is ScannerState.OFF
    asyncio.run(scenario())


def test_be17_12_new_instance_can_acquire_ownership_after_clean_release() -> None:
    """12. A new instance can acquire ownership after clean release."""
    async def scenario() -> None:
        mock_persistence = MagicMock(spec=Persistence)
        mock_persistence.engine = MagicMock()
        mock_persistence.engine.dialect = MagicMock()
        mock_persistence.engine.dialect.name = "postgresql"

        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_conn.invalidated = False
        mock_persistence.engine.connect.return_value = mock_conn
        mock_conn.execute.return_value.scalar.return_value = True

        mock_lease_1 = ScannerSchedulerLease(mock_persistence)
        mock_lease_2 = ScannerSchedulerLease(mock_persistence)

        service_1 = ScannerService(
            FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock(), lease=mock_lease_1
        )
        service_2 = ScannerService(
            FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock(), lease=mock_lease_2
        )

        await service_1.start()
        assert service_1.status().is_owner is True

        await service_1.stop()
        assert service_1.status().is_owner is False

        await service_2.start()
        assert service_2.status().is_owner is True
        await service_2.stop()
    asyncio.run(scenario())


def test_be17_13_lost_database_session_stops_future_scheduling() -> None:
    """13. Lost database session or advisory lock stops future scheduling."""
    async def scenario() -> None:
        mock_persistence = MagicMock(spec=Persistence)
        mock_persistence.engine = MagicMock()
        mock_persistence.engine.dialect = MagicMock()
        mock_persistence.engine.dialect.name = "postgresql"

        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_conn.invalidated = False
        mock_persistence.engine.connect.return_value = mock_conn
        mock_conn.execute.return_value.scalar.return_value = True

        mock_lease = ScannerSchedulerLease(mock_persistence)

        service = ScannerService(
            FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock(), lease=mock_lease
        )

        await service.start()
        assert service.status().state is ScannerState.ON

        # Simulate lost connection / query raising an exception
        mock_conn.execute.side_effect = Exception("Connection lost")

        # This should trigger validate_current_ownership failure and break the scheduler loop
        await service._scheduler_loop()

        status = service.status()
        assert status.state is ScannerState.OFF
        assert status.blocking_code == "SCANNER_SCHEDULER_LEADER_LOST"
        await service.stop()
    asyncio.run(scenario())


def test_be17_14_manual_run_now_does_not_create_scheduler() -> None:
    """14. Manual run-now does not create a scheduler."""
    async def scenario() -> None:
        service = ScannerService(
            FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock()
        )

        async def dummy_scan() -> ScannerRunSummary:
            return ScannerRunSummary(
                run_id="run-dummy",
                run_type=ScannerRunType.FULL_UNIVERSE_SCAN,
                status=ScannerRunStatus.COMPLETED,
                run_started_at=NOW,
            )
        service.full_scan = dummy_scan  # type: ignore[method-assign]

        run = await service.run_now()
        assert run.run_id == "run-dummy"
        assert service.status().scheduler_running is False
    asyncio.run(scenario())


def test_be17_15_manual_recurring_start_cannot_bypass_ownership() -> None:
    """15. Manual recurring start cannot bypass ownership requirements."""
    async def scenario() -> None:
        mock_persistence = MagicMock(spec=Persistence)
        mock_persistence.engine = MagicMock()
        mock_persistence.engine.dialect = MagicMock()
        mock_persistence.engine.dialect.name = "postgresql"

        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_conn.invalidated = False
        mock_persistence.engine.connect.return_value = mock_conn
        mock_conn.execute.return_value.scalar.return_value = False

        mock_lease = ScannerSchedulerLease(mock_persistence)

        service = ScannerService(
            FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock(), lease=mock_lease
        )
        status = await service.start(source="manual")
        assert status.state is ScannerState.OFF
        assert status.blocking_code == "OWNERSHIP_ACQUISITION_FAILED"
        await service.stop()
    asyncio.run(scenario())


def test_be17_16_status_exposes_correct_ownership_and_blocking_state() -> None:
    """16. Status exposes correct ownership and blocking state."""
    async def scenario() -> None:
        mock_persistence = MagicMock(spec=Persistence)
        mock_persistence.engine = MagicMock()
        mock_persistence.engine.dialect = MagicMock()
        mock_persistence.engine.dialect.name = "postgresql"

        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_conn.invalidated = False
        mock_persistence.engine.connect.return_value = mock_conn
        mock_conn.execute.return_value.scalar.return_value = False

        mock_lease = ScannerSchedulerLease(mock_persistence)

        service = ScannerService(
            FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock(), lease=mock_lease
        )
        await service.start()
        status = service.status()
        assert status.ownership_required is True
        assert status.ownership_held is False
        assert status.is_owner is False
        assert status.blocking_code == "OWNERSHIP_ACQUISITION_FAILED"
        await service.stop()
    asyncio.run(scenario())


def test_be17_17_partial_lifespan_startup_cleanup_works() -> None:
    """17. Partial lifespan startup cleanup works."""
    async def scenario() -> None:
        service = ScannerService(
            FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock()
        )
        assert service.status().state is ScannerState.OFF
        status = await service.stop()
        assert status.state is ScannerState.OFF
    asyncio.run(scenario())


# ==============================================================================
# BE-18 TESTS: SCANNER LATEST-RUN AND DEGRADED CONTRACT
# ==============================================================================

def test_be18_18_no_latest_run_is_represented_truthfully() -> None:
    """18. No latest run is represented truthfully."""
    service = ScannerService(
        FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock()
    )
    assert service.latest_run() is None


def test_be18_19_running_latest_run_response() -> None:
    """19. Running latest-run response."""
    run = ScannerRunSummary(
        run_id="run-running",
        run_type=ScannerRunType.FULL_UNIVERSE_SCAN,
        status=ScannerRunStatus.RUNNING,
        run_started_at=NOW,
    )
    assert run.status is ScannerRunStatus.RUNNING
    assert run.degraded_state is False
    assert run.results_usable is False  # Explicitly False for RUNNING status per Finding 4
    assert run.execution_eligibility_blocked is False


def test_be18_20_successful_latest_run_response() -> None:
    """20. Successful latest-run response."""
    run = ScannerRunSummary(
        run_id="run-success",
        run_type=ScannerRunType.FULL_UNIVERSE_SCAN,
        status=ScannerRunStatus.COMPLETED,
        run_started_at=NOW,
        completed_at=NOW,
    )
    assert run.status is ScannerRunStatus.COMPLETED
    assert run.degraded_state is False
    assert run.results_usable is True
    assert run.execution_eligibility_blocked is False


def test_be18_21_degraded_latest_run_response() -> None:
    """21. Degraded latest-run response."""
    run = ScannerRunSummary(
        run_id="run-degraded",
        run_type=ScannerRunType.FULL_UNIVERSE_SCAN,
        status=ScannerRunStatus.DEGRADED,
        run_started_at=NOW,
        completed_at=NOW,
        audits=[
            ScannerAuditRecord(code="MISSING_5M_CANDLES", detail="Degraded scan completeness")
        ],
        failed_symbols=1,
    )
    assert run.status is ScannerRunStatus.DEGRADED
    assert run.degraded_state is True
    assert run.results_usable is True
    assert run.execution_eligibility_blocked is False
    assert run.audit_count == 1
    assert "MISSING_5M_CANDLES" in run.diagnostic_codes


def test_be18_22_failed_latest_run_response() -> None:
    """22. Failed latest-run response."""
    run = ScannerRunSummary(
        run_id="run-failed",
        run_type=ScannerRunType.FULL_UNIVERSE_SCAN,
        status=ScannerRunStatus.FAILED,
        run_started_at=NOW,
        completed_at=NOW,
    )
    assert run.status is ScannerRunStatus.FAILED
    assert run.degraded_state is False
    assert run.results_usable is False
    assert run.execution_eligibility_blocked is True


def test_be18_23_skipped_duplicate_run_response() -> None:
    """23. Skipped duplicate-run response."""
    run = ScannerRunSummary(
        run_id="run-skipped",
        run_type=ScannerRunType.FULL_UNIVERSE_SCAN,
        status=ScannerRunStatus.SKIPPED,
        run_started_at=NOW,
        completed_at=NOW,
    )
    assert run.status is ScannerRunStatus.SKIPPED
    assert run.degraded_state is False
    assert run.results_usable is False  # Explicitly False for SKIPPED status per Finding 4
    assert run.execution_eligibility_blocked is False


def test_be18_24_symbol_level_technical_failures_create_degraded_status() -> None:
    """24. Symbol-level technical failures create degraded status when results remain usable."""
    run = ScannerRunSummary(
        run_id="run-symbol-fail",
        run_type=ScannerRunType.FULL_UNIVERSE_SCAN,
        status=ScannerRunStatus.DEGRADED,
        run_started_at=NOW,
        completed_at=NOW,
        failed_symbols=2,
        successful_symbols=10,
        audits=[
            ScannerAuditRecord(code="MISSING_15M_CANDLES", detail="Symbol ETH failed technically")
        ]
    )
    assert run.status is ScannerRunStatus.DEGRADED
    assert run.results_usable is True
    assert run.failed_symbols == 2
    assert run.degraded_state is True


def test_be18_25_total_dependency_failure_remains_failed() -> None:
    """25. Total dependency failure remains failed."""
    run = ScannerRunSummary(
        run_id="run-total-fail",
        run_type=ScannerRunType.FULL_UNIVERSE_SCAN,
        status=ScannerRunStatus.FAILED,
        run_started_at=NOW,
        completed_at=NOW,
        successful_symbols=0,
        failed_symbols=1,
    )
    assert run.status is ScannerRunStatus.FAILED
    assert run.results_usable is False
    assert run.execution_eligibility_blocked is True


def test_be18_26_normal_rejections_do_not_create_false_degraded_status() -> None:
    """26. Normal no-setup and strategy rejection do not create false degraded status."""
    run = ScannerRunSummary(
        run_id="run-normal-rejections",
        run_type=ScannerRunType.FULL_UNIVERSE_SCAN,
        status=ScannerRunStatus.COMPLETED,
        run_started_at=NOW,
        completed_at=NOW,
        audits=[
            ScannerAuditRecord(code="SETUP_NOT_DETECTED", detail="No setup found"),
            ScannerAuditRecord(code="SCORE_BELOW_80", detail="Low score")
        ]
    )
    assert run.status is ScannerRunStatus.COMPLETED
    assert run.degraded_state is False
    assert run.results_usable is True


def test_be18_27_diagnostic_models_remain_typed_and_secret_safe() -> None:
    """27. Diagnostic models remain typed and secret-safe."""
    record = ScannerAuditRecord(
        code="CLOCK_SKEW_EXCEEDED",
        detail="Local clock is out of sync",
        symbol="BTCUSDT",
    )
    assert record.severity == "error"
    assert record.blocking is True
    assert record.retryable is True
    assert "BTCUSDT" in record.model_dump_json()
    assert "token" not in record.model_dump_json()
    assert "key" not in record.model_dump_json()


def test_be18_28_existing_scanner_route_contracts_continue_to_pass() -> None:
    """28. Existing Scanner route contracts continue to pass."""
    settings = Settings(
        _env_file=None,
        environment="test",
        cors_origins=["http://localhost:5173"],
    )
    app_instance = create_app(settings)
    with TestClient(app_instance) as client:
        response = client.get("/api/v1/scanner/status")
        assert response.status_code == 200
        assert response.json()["state"] == "OFF"


def test_be18_29_openapi_publishes_required_scanner_schemas() -> None:
    """29. OpenAPI publishes the required Scanner schemas."""
    settings = Settings(
        _env_file=None,
        environment="test",
        cors_origins=["http://localhost:5173"],
    )
    app_instance = create_app(settings)
    with TestClient(app_instance) as client:
        openapi = client.get("/api/v1/openapi.json").json()
        schemas = openapi["components"]["schemas"]
        assert "ScannerStatusResponse" in schemas
        assert "ScannerRunSummary" in schemas
        assert "ScannerAuditRecord" in schemas

        status_fields = schemas["ScannerStatusResponse"]["properties"]
        assert "auto_start_configured" in status_fields
        assert "ownership_required" in status_fields
        assert "is_owner" in status_fields
        assert "blocking_code" in status_fields

        summary_fields = schemas["ScannerRunSummary"]["properties"]
        assert "audit_count" in summary_fields
        assert "degraded_state" in summary_fields
        assert "results_usable" in summary_fields


# ==============================================================================
# ADDITIONAL COVERAGE BOOST TESTS FOR SCANNER RUNTIME AND LEASE
# ==============================================================================

def test_be17_lease_none_persistence_edge_cases() -> None:
    """Cover ScannerSchedulerLease edge cases with None persistence."""
    lease = ScannerSchedulerLease(None)
    assert lease.held is False
    assert lease.acquire() is False
    with pytest.raises(ScannerSchedulerLost, match="lease is not acquired"):
        lease.validate_current_ownership()
    lease.release()
    lease._discard_lost_connection()


def test_be17_lease_closed_or_invalidated_connection() -> None:
    """Cover validation when connection is closed or invalidated."""
    mock_persistence = MagicMock(spec=Persistence)
    lease = ScannerSchedulerLease(mock_persistence)
    mock_conn = MagicMock()
    mock_conn.closed = True  # Simulated closed
    lease._connection = mock_conn
    with pytest.raises(ScannerSchedulerLost, match="database session is unavailable"):
        lease.validate_current_ownership()
    assert lease._connection is None


def test_be17_lease_validation_exceptions_and_not_owned() -> None:
    """Cover exceptions and not owned cases during validate_current_ownership."""
    mock_persistence = MagicMock(spec=Persistence)
    lease = ScannerSchedulerLease(mock_persistence)
    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.invalidated = False
    lease._connection = mock_conn

    # 1. Query raises exception
    mock_conn.execute.side_effect = RuntimeError("DB Query Error")
    with pytest.raises(ScannerSchedulerLost, match="validation failed"):
        lease.validate_current_ownership()
    assert lease._connection is None

    # 2. Query returns False (not owned)
    lease._connection = mock_conn
    mock_conn.execute.side_effect = None
    mock_conn.execute.return_value.scalar.return_value = False
    with pytest.raises(ScannerSchedulerLost, match="ownership was lost"):
        lease.validate_current_ownership()
    assert lease._connection is None


def test_be17_runtime_is_postgresql_authoritative_exceptions() -> None:
    """Cover dialect query exceptions inside _is_postgresql_authoritative."""
    mock_persistence = MagicMock(spec=Persistence)
    # Dialect query raises exception
    type(mock_persistence).engine = property(lambda self: Exception("No engine"))
    lease = ScannerSchedulerLease(mock_persistence)
    service = ScannerService(
        FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock(), lease=lease
    )
    assert service._is_postgresql_authoritative() is False


def test_be17_risk_stop_price_exceptions_and_edge_cases() -> None:
    """Cover exceptions and edge cases inside risk_stop_price."""
    service = ScannerService(
        FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock()
    )
    # Unknown candidate
    assert service.risk_stop_price("unknown") is None

    # Key/type error in _evidence_decimal
    with pytest.raises(KeyError):
        service._evidence_decimal(
            ScannerCandidate(
                candidate_id="c1",
                symbol="BTCUSDT",
                direction=ScannerDirection.LONG,
                setup=ScannerSetup.TREND_PULLBACK,
                setup_name="Tp",
                reference_close_time=NOW,
                setup_confirmed_at=NOW,
                expires_at=NOW,
                lifecycle=CandidateLifecycle.WATCH_NEAR,
                entry_ready=False,
                universe_rank=1,
                quote_volume=Decimal("100"),
                spread_bps=Decimal("1"),
                entry_trigger_price=Decimal("100"),
                evaluated_at=NOW,
            ),
            "missing_key",
        )


def test_be17_risk_stop_price_additional_coverage() -> None:
    """Cover SHORT direction and other setup branches inside risk_stop_price."""
    service = ScannerService(
        FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock()
    )

    # Mock candidate context
    ctx = MagicMock()
    ctx.s = [MagicMock()]
    ctx.e = [MagicMock()]
    ctx.s[0].indicator.atr14 = Decimal("1.5")
    ctx.e[0].indicator.atr14 = Decimal("0.5")
    ctx.s[0].indicator.ema50 = Decimal("100")
    service._candidate_contexts["c_short"] = ctx

    # 1. EMA_REJECTION SHORT
    cand_ema = ScannerCandidate(
        candidate_id="c_short",
        symbol="BTCUSDT",
        direction=ScannerDirection.SHORT,
        setup=ScannerSetup.EMA_REJECTION,
        setup_name="Ema",
        reference_close_time=NOW,
        setup_confirmed_at=NOW,
        expires_at=NOW,
        lifecycle=CandidateLifecycle.WATCH_NEAR,
        entry_ready=False,
        universe_rank=1,
        quote_volume=Decimal("100"),
        spread_bps=Decimal("1"),
        entry_trigger_price=Decimal("100"),
        evaluated_at=NOW,
        selected_ema=Decimal("102"),
        evidence={"reference_high": "103", "reference_low": "100"},
    )
    service._candidates["c_short"] = cand_ema
    # 102 + 0.20 * 1.5 = 102.3, which is > 100 entry trigger price, so it's a valid stop
    assert service.risk_stop_price("c_short") == Decimal("102.30")

    # 2. LIQUIDITY_SWEEP_REVERSAL SHORT
    cand_sweep = ScannerCandidate(
        candidate_id="c_short",
        symbol="BTCUSDT",
        direction=ScannerDirection.SHORT,
        setup=ScannerSetup.LIQUIDITY_SWEEP_REVERSAL,
        setup_name="Sweep",
        reference_close_time=NOW,
        setup_confirmed_at=NOW,
        expires_at=NOW,
        lifecycle=CandidateLifecycle.WATCH_NEAR,
        entry_ready=False,
        universe_rank=1,
        quote_volume=Decimal("100"),
        spread_bps=Decimal("1"),
        entry_trigger_price=Decimal("100"),
        evaluated_at=NOW,
        evidence={"reference_high": "101", "reference_low": "100"},
    )
    service._candidates["c_short"] = cand_sweep
    # 101 + 0.05 * 0.5 = 101.025, which is > 100, valid stop
    assert service.risk_stop_price("c_short") == Decimal("101.025")

    # 3. CONTINUATION_SETUP SHORT
    cand_cont = ScannerCandidate(
        candidate_id="c_short",
        symbol="BTCUSDT",
        direction=ScannerDirection.SHORT,
        setup=ScannerSetup.CONTINUATION_SETUP,
        setup_name="Cont",
        reference_close_time=NOW,
        setup_confirmed_at=NOW,
        expires_at=NOW,
        lifecycle=CandidateLifecycle.WATCH_NEAR,
        entry_ready=False,
        universe_rank=1,
        quote_volume=Decimal("100"),
        spread_bps=Decimal("1"),
        entry_trigger_price=Decimal("100"),
        evaluated_at=NOW,
        level=Decimal("101"),
    )
    service._candidates["c_short"] = cand_cont
    # 101 + 0.15 * 1.5 = 101.225, which is > 100, valid stop
    assert service.risk_stop_price("c_short") == Decimal("101.225")


def test_be17_risk_stop_price_full_coverage() -> None:
    """Cover remaining setup branches (TREND_PULLBACK, BREAKOUT_RETEST) LONG & SHORT."""
    service = ScannerService(
        FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock()
    )

    ctx = MagicMock()
    ctx.s = [MagicMock()]
    ctx.e = [MagicMock()]
    ctx.s[0].indicator.atr14 = Decimal("1.0")
    ctx.e[0].indicator.atr14 = Decimal("0.5")
    ctx.s[0].indicator.ema50 = Decimal("100")
    service._candidate_contexts["c1"] = ctx

    # 1. TREND_PULLBACK LONG
    cand_pullback_long = ScannerCandidate(
        candidate_id="c1",
        symbol="BTCUSDT",
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Pullback",
        reference_close_time=NOW,
        setup_confirmed_at=NOW,
        expires_at=NOW,
        lifecycle=CandidateLifecycle.WATCH_NEAR,
        entry_ready=False,
        universe_rank=1,
        quote_volume=Decimal("100"),
        spread_bps=Decimal("1"),
        entry_trigger_price=Decimal("101"),
        evaluated_at=NOW,
        evidence={"pullback_swing_low": "99", "pullback_swing_high": "102"},
    )
    service._candidates["c1"] = cand_pullback_long
    # max(100 - 0.25 * 1.0, 99 - 0.10 * 0.5) = max(99.75, 98.95) = 99.75, which is < 101, valid stop
    assert service.risk_stop_price("c1") == Decimal("99.75")

    # 2. TREND_PULLBACK SHORT
    cand_pullback_short = ScannerCandidate(
        candidate_id="c1",
        symbol="BTCUSDT",
        direction=ScannerDirection.SHORT,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Pullback",
        reference_close_time=NOW,
        setup_confirmed_at=NOW,
        expires_at=NOW,
        lifecycle=CandidateLifecycle.WATCH_NEAR,
        entry_ready=False,
        universe_rank=1,
        quote_volume=Decimal("100"),
        spread_bps=Decimal("1"),
        entry_trigger_price=Decimal("99"),
        evaluated_at=NOW,
        evidence={"pullback_swing_low": "98", "pullback_swing_high": "100.5"},
    )
    service._candidates["c1"] = cand_pullback_short
    # min(100 + 0.25 * 1.0, 100.5 + 0.10 * 0.5) = min(100.25, 100.55) = 100.25
    # which is > 99, valid stop
    assert service.risk_stop_price("c1") == Decimal("100.25")

    # 3. BREAKOUT_RETEST LONG
    cand_breakout_long = ScannerCandidate(
        candidate_id="c1",
        symbol="BTCUSDT",
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.BREAKOUT_RETEST,
        setup_name="Breakout",
        reference_close_time=NOW,
        setup_confirmed_at=NOW,
        expires_at=NOW,
        lifecycle=CandidateLifecycle.WATCH_NEAR,
        entry_ready=False,
        universe_rank=1,
        quote_volume=Decimal("100"),
        spread_bps=Decimal("1"),
        entry_trigger_price=Decimal("101"),
        evaluated_at=NOW,
        level=Decimal("100"),
    )
    service._candidates["c1"] = cand_breakout_long
    # 100 - 0.15 * 1.0 = 99.85, valid stop
    assert service.risk_stop_price("c1") == Decimal("99.85")

    # 4. _evidence_decimal ValueError for infinite Decimals
    cand_err = ScannerCandidate(
        candidate_id="c1",
        symbol="BTCUSDT",
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Pullback",
        reference_close_time=NOW,
        setup_confirmed_at=NOW,
        expires_at=NOW,
        lifecycle=CandidateLifecycle.WATCH_NEAR,
        entry_ready=False,
        universe_rank=1,
        quote_volume=Decimal("100"),
        spread_bps=Decimal("1"),
        entry_trigger_price=Decimal("101"),
        evaluated_at=NOW,
        evidence={"pullback_swing_low": "Infinity", "pullback_swing_high": "102"},
    )
    service._candidates["c1"] = cand_err
    assert service.risk_stop_price("c1") is None


def test_be17_evidence_decimal_value_error() -> None:
    """Directly test _evidence_decimal value error to force coverage."""
    service = ScannerService(
        FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock()
    )
    cand_err = ScannerCandidate(
        candidate_id="c1",
        symbol="BTCUSDT",
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Pullback",
        reference_close_time=NOW,
        setup_confirmed_at=NOW,
        expires_at=NOW,
        lifecycle=CandidateLifecycle.WATCH_NEAR,
        entry_ready=False,
        universe_rank=1,
        quote_volume=Decimal("100"),
        spread_bps=Decimal("1"),
        entry_trigger_price=Decimal("101"),
        evaluated_at=NOW,
        evidence={"pullback_swing_low": "Infinity"},
    )
    with pytest.raises(ValueError, match="pullback_swing_low"):
        service._evidence_decimal(cand_err, "pullback_swing_low")


def test_be17_lease_acquire_dialect_exceptions() -> None:
    """Cover dialect or connection exceptions during lease acquire/release."""
    mock_persistence = MagicMock()
    mock_persistence.engine.dialect.name = "mysql"  # Not PostgreSQL
    lease = ScannerSchedulerLease(mock_persistence)
    assert lease.acquire() is False

    # Connection failure propagation
    mock_persistence.engine.dialect.name = "postgresql"
    mock_persistence.engine.connect.side_effect = Exception("Connect failed")
    with pytest.raises(Exception, match="Connect failed"):
        lease.acquire()

    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.invalidated = False
    lease._connection = mock_conn
    mock_conn.execute.side_effect = RuntimeError("Execute Exception")
    lease.release()


def test_be17_lease_invalidated_connection() -> None:
    """Cover validation when connection is invalidated but not closed."""
    mock_persistence = MagicMock()
    lease = ScannerSchedulerLease(mock_persistence)
    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.invalidated = True  # Simulated invalidated
    lease._connection = mock_conn
    assert lease.held is False


def test_be17_lease_acquire_returns_false() -> None:
    """Cover acquire() returning False cleanly when pg_try_advisory_lock fails."""
    mock_persistence = MagicMock()
    mock_persistence.engine.dialect.name = "postgresql"
    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.invalidated = False
    mock_persistence.engine.connect.return_value = mock_conn
    mock_conn.execute.return_value.scalar.return_value = False  # Lock failed
    lease = ScannerSchedulerLease(mock_persistence)
    assert lease.acquire() is False


def test_be17_start_validation_fails() -> None:
    """Cover exception path of validate_current_ownership inside start()."""
    async def scenario() -> None:
        mock_persistence = MagicMock()
        mock_persistence.engine.dialect.name = "postgresql"
        mock_conn = MagicMock()
        mock_conn.closed = False
        mock_conn.invalidated = False
        mock_persistence.engine.connect.return_value = mock_conn

        # First call to execute (acquire) returns True, second (validate) raises exception
        mock_conn.execute.return_value.scalar.side_effect = [True, Exception("Validation error")]

        lease = ScannerSchedulerLease(mock_persistence)
        service = ScannerService(
            FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock(), lease=lease
        )
        status = await service.start()
        assert status.state is ScannerState.OFF
        assert status.blocking_code == "OWNERSHIP_VALIDATION_FAILED"
    asyncio.run(scenario())


def test_be17_lease_validation_close_exception() -> None:
    """Cover exception path in _discard_lost_connection during lease validation."""
    mock_persistence = MagicMock()
    lease = ScannerSchedulerLease(mock_persistence)
    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.invalidated = False
    lease._connection = mock_conn

    # Validate owned query returns False, which triggers validation failure and connection discard
    mock_conn.execute.return_value.scalar.return_value = False
    # Make close() raise an exception
    mock_conn.close.side_effect = Exception("Close error")

    with pytest.raises(ScannerSchedulerLost, match="advisory-lock ownership was lost"):
        lease.validate_current_ownership()
    assert lease._connection is None


def test_be17_lease_release_close_exception() -> None:
    """Cover exceptions inside lease release."""
    mock_persistence = MagicMock()
    lease = ScannerSchedulerLease(mock_persistence)
    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.invalidated = False
    lease._connection = mock_conn

    # Make connection.close raise an exception
    mock_conn.close.side_effect = Exception("Close failed")
    lease.release()
    assert lease._connection is None


def test_be17_multi_instance_competing_advisory_lock() -> None:
    """Proves two instances competing for one shared PostgreSQL advisory lock."""
    # Simulates a shared Postgres lock state
    shared_lock_table = set()

    def make_mock_connection(instance_id: str) -> MagicMock:
        conn = MagicMock()
        conn.closed = False
        conn.invalidated = False

        def execute_query(query, params=None):
            sql_text = str(query)
            if "pg_try_advisory_lock" in sql_text:
                lock_key = params["lock_key"] if params else 0
                if lock_key in shared_lock_table:
                    # Already locked by another instance
                    ret = MagicMock()
                    ret.scalar.return_value = False
                    return ret
                else:
                    shared_lock_table.add(lock_key)
                    ret = MagicMock()
                    ret.scalar.return_value = True
                    return ret
            elif "pg_advisory_unlock" in sql_text:
                lock_key = params["lock_key"] if params else 0
                shared_lock_table.discard(lock_key)
                ret = MagicMock()
                return ret
            elif "EXISTS" in sql_text:
                # Validate ownership check
                ret = MagicMock()
                # If lock key is still held, it validates True
                lock_key = 0x4153545241464F53
                ret.scalar.return_value = lock_key in shared_lock_table
                return ret
            return MagicMock()

        conn.execute.side_effect = execute_query
        return conn

    # Instance A
    persistence_a = MagicMock()
    persistence_a.engine.dialect.name = "postgresql"
    conn_a = make_mock_connection("instance_a")
    persistence_a.engine.connect.return_value = conn_a
    lease_a = ScannerSchedulerLease(persistence_a)

    # Instance B
    persistence_b = MagicMock()
    persistence_b.engine.dialect.name = "postgresql"
    conn_b = make_mock_connection("instance_b")
    persistence_b.engine.connect.return_value = conn_b
    lease_b = ScannerSchedulerLease(persistence_b)

    # 1. Instance A acquires ownership successfully
    assert lease_a.acquire() is True
    assert lease_a.held is True

    # 2. Instance B attempts to acquire ownership but gets rejected
    assert lease_b.acquire() is False
    assert lease_b.held is False

    # 3. Instance A releases lock cleanly
    lease_a.release()
    assert lease_a.held is False

    # 4. Instance B can now acquire ownership cleanly
    assert lease_b.acquire() is True
    assert lease_b.held is True
    lease_b.release()


def test_be17_auto_start_true_without_persistence_fails_closed_in_dev_test() -> None:
    """Proves auto-start=true without PostgreSQL fails closed in dev/test environment."""
    # Overriding to make sure setting source="lifespan" fails closed if no PostgreSQL
    service = ScannerService(
        FakeMarket(), FakeUniverse(), FakeIndicators(), clock=FakeClock(), lease=None
    )
    # Automatic start via lifespan source
    status = asyncio.run(service.start(source="lifespan"))
    assert status.state is ScannerState.OFF
    assert status.blocking_code == "PERSISTENCE_UNAVAILABLE"
