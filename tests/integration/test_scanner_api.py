"""Scanner API integration tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

import app.main
from app.api.v1.dependencies import get_scanner_service
from app.core.config import Settings
from app.main import create_app
from app.schemas.scanner import (
    CandidateLifecycle,
    ScannerAuditRecord,
    ScannerCandidate,
    ScannerDirection,
    ScannerGrade,
    ScannerRunStatus,
    ScannerRunSummary,
    ScannerRunType,
    ScannerSetup,
    ScannerState,
    ScannerStatusResponse,
)

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class StubScanner:
    def __init__(self) -> None:
        self.state = ScannerState.OFF
        self.candidate = ScannerCandidate(
            candidate_id="a" * 64,
            symbol="BTCUSDT",
            direction=ScannerDirection.LONG,
            setup=ScannerSetup.TREND_PULLBACK,
            setup_name="Trend Pullback",
            reference_close_time=NOW,
            setup_confirmed_at=NOW,
            expires_at=NOW,
            lifecycle=CandidateLifecycle.WATCH_NEAR,
            score=84,
            confidence=75,
            grade=ScannerGrade.B_PLUS,
            entry_ready=False,
            universe_rank=1,
            quote_volume=Decimal("1000000000"),
            spread_bps=Decimal("1"),
            entry_trigger_price=Decimal("101"),
            evaluated_at=NOW,
        )
        self.run = ScannerRunSummary(
            run_id="run-1",
            run_type=ScannerRunType.FULL_UNIVERSE_SCAN,
            status=ScannerRunStatus.COMPLETED,
            run_started_at=NOW,
            completed_at=NOW,
            universe_size=1,
            evaluated_symbols=1,
            successful_symbols=1,
            failed_symbols=0,
            discovered_candidates=1,
            selected_candidates=1,
            audits=[
                ScannerAuditRecord(
                    code="SETUP_NOT_DETECTED",
                    detail="No approved deterministic setup matched",
                    symbol="ETHUSDT",
                    timeframe="15m",
                )
            ],
        )

    def status(self) -> ScannerStatusResponse:
        return ScannerStatusResponse(
            state=self.state,
            active_candidate_count=1,
            latest_run=self.run,
        )

    async def start(self, source: str = "manual") -> ScannerStatusResponse:
        self.state = ScannerState.ON
        return self.status()

    async def stop(self) -> ScannerStatusResponse:
        self.state = ScannerState.OFF
        return self.status()

    async def run_now(self) -> ScannerRunSummary:
        return self.run

    def candidates(self) -> list[ScannerCandidate]:
        return [self.candidate]

    def latest_run(self) -> ScannerRunSummary | None:
        return self.run


def test_scanner_api_contract(settings) -> None:  # type: ignore[no-untyped-def]
    stub = StubScanner()
    app = create_app(settings)
    app.dependency_overrides[get_scanner_service] = lambda: stub
    with TestClient(app) as client:
        assert client.get("/api/v1/scanner/status").json()["state"] == "OFF"
        assert client.post("/api/v1/scanner/start").json()["state"] == "ON"
        assert client.post("/api/v1/scanner/stop").json()["state"] == "OFF"
        assert client.post("/api/v1/scanner/run-now").json()["status"] == "COMPLETED"
        response = client.get(
            "/api/v1/scanner/candidates",
            params={
                "symbol": "btcusdt",
                "direction": "LONG",
                "setup": "trend_pullback",
                "lifecycle": "WATCH_NEAR",
            },
        )
        assert response.status_code == 200
        assert response.json()["count"] == 1
        assert response.json()["summary"]["state"] == "OFF"
        assert response.json()["summary"]["run_status"] == "COMPLETED"
        assert response.json()["summary"]["evaluated_symbols"] == 1
        assert response.json()["summary"]["audits"][0]["code"] == "SETUP_NOT_DETECTED"
        assert client.get("/api/v1/scanner/runs/latest").json()["run_id"] == "run-1"
        assert client.get(
            "/api/v1/scanner/candidates", params={"symbol": "***"}
        ).status_code == 422


def test_latest_run_404(settings) -> None:  # type: ignore[no-untyped-def]
    stub = StubScanner()
    stub.latest_run = lambda: None  # type: ignore[method-assign]
    app = create_app(settings)
    app.dependency_overrides[get_scanner_service] = lambda: stub
    with TestClient(app) as client:
        assert client.get("/api/v1/scanner/runs/latest").status_code == 404
        response = client.get("/api/v1/scanner/candidates")
        assert response.status_code == 200
        assert response.json()["summary"]["run_status"] is None


def test_app_startup_auto_starts_scanner(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class StartupStubScanner(StubScanner):
        def __init__(self) -> None:
            super().__init__()
            self.start_calls = 0

        async def start(self, source: str = "manual") -> ScannerStatusResponse:
            self.start_calls += 1
            await asyncio.sleep(0)
            self.state = ScannerState.ON
            return self.status()

    stub = StartupStubScanner()
    monkeypatch.setattr(app.main, "get_scanner_service", lambda: stub)
    settings = Settings(
        _env_file=None,
        environment="test",
        cors_origins=["http://localhost:5173"],
        scanner_auto_start=True,
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/scanner/status")

    assert response.status_code == 200
    assert stub.start_calls == 1
