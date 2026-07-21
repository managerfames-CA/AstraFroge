"""Risk API integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from app.api.v1.dependencies import get_risk_service
from app.main import create_app
from app.schemas.risk import (
    KillSwitchState,
    RiskAssessment,
    RiskAssessmentList,
    RiskDecision,
    RiskEngineState,
    RiskStatusResponse,
    RiskSummary,
)
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.signals import SignalLifecycle

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class StubRisk:
    def __init__(self) -> None:
        self.assessment = RiskAssessment(
            signal_id="a" * 64,
            symbol="BTCUSDT",
            direction=ScannerDirection.LONG,
            setup=ScannerSetup.TREND_PULLBACK,
            setup_name="Trend Pullback",
            signal_lifecycle=SignalLifecycle.ACTIVE,
            grade=ScannerGrade.A_PLUS,
            score=92,
            confidence=80,
            decision=RiskDecision.APPROVED,
            approved_for_execution=True,
            entry_trigger_price=Decimal("101"),
            current_margin_exposure_usdt=Decimal("0"),
            max_open_trades_limit=4,
            updated_at=NOW,
        )

    def status(self) -> RiskStatusResponse:
        return RiskStatusResponse(
            state=RiskEngineState.READY,
            signal_engine_state="READY",
            daily_loss_limit_percent=Decimal("3"),
            daily_profit_lock_percent=Decimal("5"),
            current_margin_exposure_usdt=Decimal("0"),
            max_open_trades_limit=4,
            available_tracking_slots=3,
            emergency_kill_switch=KillSwitchState.OFFLINE,
            updated_at=NOW,
            summary=RiskSummary(approved=1),
        )

    def assessments(self) -> RiskAssessmentList:
        return RiskAssessmentList(count=1, assessments=[self.assessment])


def test_risk_api_contract(settings) -> None:  # type: ignore[no-untyped-def]
    stub = StubRisk()
    app = create_app(settings)
    app.dependency_overrides[get_risk_service] = lambda: stub
    with TestClient(app) as client:
        status = client.get("/api/v1/risk/status")
        assert status.status_code == 200
        assert status.json()["state"] == "READY"
        response = client.get(
            "/api/v1/risk/assessments",
            params={
                "symbol": "btcusdt",
                "direction": "LONG",
                "setup": "trend_pullback",
                "grade": "A+",
                "lifecycle": "ACTIVE",
                "decision": "APPROVED",
            },
        )
        assert response.status_code == 200
        assert response.json()["count"] == 1
        assert client.get("/api/v1/risk/assessments", params={"symbol": "***"}).status_code == 422
