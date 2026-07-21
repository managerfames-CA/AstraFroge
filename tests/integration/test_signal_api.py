"""Signal API integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from app.api.v1.dependencies import get_signal_service
from app.main import create_app
from app.schemas.scanner import CandidateLifecycle, ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.signals import (
    SignalEngineState,
    SignalLifecycle,
    SignalRecord,
    SignalRecordList,
    SignalStatusResponse,
    SignalSummary,
)

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class StubSignals:
    def __init__(self) -> None:
        self.record = SignalRecord(
            signal_id="a" * 64,
            candidate_id="a" * 64,
            symbol="BTCUSDT",
            direction=ScannerDirection.LONG,
            setup=ScannerSetup.TREND_PULLBACK,
            setup_name="Trend Pullback",
            lifecycle=SignalLifecycle.ACTIVE,
            scanner_lifecycle=CandidateLifecycle.QUALIFIED,
            grade=ScannerGrade.A_PLUS,
            score=92,
            confidence=80,
            entry_ready=True,
            entry_trigger_price=Decimal("101"),
            reference_close_time=NOW,
            setup_confirmed_at=NOW,
            expires_at=NOW,
            qualification_expires_at=NOW,
            evaluated_at=NOW,
            universe_rank=1,
            quote_volume=Decimal("1000000000"),
            spread_bps=Decimal("1"),
        )

    def status(self) -> SignalStatusResponse:
        return SignalStatusResponse(
            state=SignalEngineState.READY,
            scanner_state="ON",
            active_signal_count=1,
            watch_signal_count=0,
            terminal_signal_count=0,
            updated_at=NOW,
            latest_scanner_run_at=NOW,
            summary=SignalSummary(active_signals=1, a_plus_signals=1),
        )

    def signals(self) -> SignalRecordList:
        return SignalRecordList(count=1, signals=[self.record])


def test_signal_api_contract(settings) -> None:  # type: ignore[no-untyped-def]
    stub = StubSignals()
    app = create_app(settings)
    app.dependency_overrides[get_signal_service] = lambda: stub
    with TestClient(app) as client:
        status = client.get("/api/v1/signals/status")
        assert status.status_code == 200
        assert status.json()["state"] == "READY"
        response = client.get(
            "/api/v1/signals",
            params={
                "symbol": "btcusdt",
                "direction": "LONG",
                "setup": "trend_pullback",
                "grade": "A+",
                "lifecycle": "ACTIVE",
            },
        )
        assert response.status_code == 200
        assert response.json()["count"] == 1
        assert client.get("/api/v1/signals", params={"symbol": "***"}).status_code == 422
