"""Risk Engine unit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.core.config import Settings
from app.schemas.risk import RiskDecision, RiskEngineState
from app.schemas.scanner import CandidateLifecycle, ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.signals import (
    SignalEngineState,
    SignalLifecycle,
    SignalRecord,
    SignalRecordList,
    SignalStatusResponse,
    SignalSummary,
)
from app.services.risk import RiskService

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class StubSignals:
    def __init__(self) -> None:
        self._signals = [
            SignalRecord(
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
                stop_loss_price=Decimal("100"),
                reference_close_time=NOW,
                setup_confirmed_at=NOW,
                expires_at=NOW,
                qualification_expires_at=NOW,
                evaluated_at=NOW,
                universe_rank=1,
                quote_volume=Decimal("1000000000"),
                spread_bps=Decimal("1"),
            ),
            SignalRecord(
                signal_id="b" * 64,
                candidate_id="b" * 64,
                symbol="ETHUSDT",
                direction=ScannerDirection.SHORT,
                setup=ScannerSetup.EMA_REJECTION,
                setup_name="EMA Rejection",
                lifecycle=SignalLifecycle.WATCH,
                scanner_lifecycle=CandidateLifecycle.WATCH_NEAR,
                grade=ScannerGrade.B_PLUS,
                score=84,
                confidence=68,
                entry_ready=False,
                entry_trigger_price=Decimal("99"),
                reference_close_time=NOW,
                setup_confirmed_at=NOW,
                expires_at=NOW,
                evaluated_at=NOW,
                universe_rank=2,
                quote_volume=Decimal("900000000"),
                spread_bps=Decimal("2"),
            ),
            SignalRecord(
                signal_id="c" * 64,
                candidate_id="c" * 64,
                symbol="SOLUSDT",
                direction=ScannerDirection.LONG,
                setup=ScannerSetup.CONTINUATION_SETUP,
                setup_name="Continuation Setup",
                lifecycle=SignalLifecycle.EXPIRED,
                scanner_lifecycle=CandidateLifecycle.EXPIRED,
                grade=ScannerGrade.A,
                score=88,
                confidence=75,
                entry_ready=False,
                entry_trigger_price=Decimal("150"),
                reference_close_time=NOW,
                setup_confirmed_at=NOW,
                expires_at=NOW,
                evaluated_at=NOW,
                universe_rank=3,
                quote_volume=Decimal("800000000"),
                spread_bps=Decimal("3"),
            ),
        ]

    def status(self) -> SignalStatusResponse:
        return SignalStatusResponse(
            state=SignalEngineState.READY,
            scanner_state="ON",
            active_signal_count=1,
            watch_signal_count=1,
            terminal_signal_count=1,
            updated_at=NOW,
            latest_scanner_run_at=NOW,
            summary=SignalSummary(active_signals=1, b_plus_watch=1, expired=1),
        )

    def signals(self) -> SignalRecordList:
        return SignalRecordList(count=len(self._signals), signals=self._signals)


class StubPrivateClient:
    def account(self) -> dict[str, Any]:
        return {
            "canTrade": True,
            "totalWalletBalance": "1000",
            "availableBalance": "900",
            "totalUnrealizedProfit": "0",
            "totalInitialMargin": "0",
        }

    def positions(self) -> list[dict[str, Any]]:
        return [
            {"symbol": "BTCUSDT", "positionAmt": "0", "leverage": "10"},
            {"symbol": "ETHUSDT", "positionAmt": "0", "leverage": "10"},
            {"symbol": "SOLUSDT", "positionAmt": "0", "leverage": "10"},
        ]

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        assert start_time_ms < end_time_ms
        assert limit == 1000
        return []


def test_risk_service_projects_signal_policy() -> None:
    service = RiskService(
        StubSignals(),  # type: ignore[arg-type]
        Settings(
            _env_file=None,
            environment="test",
            risk_per_trade_percent=Decimal("1"),
            risk_daily_loss_limit_percent=Decimal("3"),
            risk_daily_profit_lock_percent=Decimal("5"),
            risk_max_open_trades=4,
            risk_max_margin_exposure_usdt=Decimal("1000"),
        ),
        StubPrivateClient(),
        now_provider=lambda: NOW,
    )

    assessments = service.assessments()
    assert assessments.count == 3
    assert assessments.assessments[0].decision is RiskDecision.APPROVED
    assert assessments.assessments[0].risk_budget_usdt == Decimal("10")
    assert assessments.assessments[0].recommended_quantity == Decimal("10")
    assert assessments.assessments[1].decision is RiskDecision.WATCH
    assert assessments.assessments[2].decision is RiskDecision.TERMINAL

    status = service.status()
    assert status.state is RiskEngineState.READY
    assert status.account_snapshot_available is True
    assert status.summary.approved == 1
    assert status.summary.watch == 1
    assert status.summary.terminal == 1
    assert status.open_position_count == 0
    assert status.available_tracking_slots == 4


def test_risk_service_waits_for_signals() -> None:
    stub = StubSignals()
    stub._signals = []
    stub.status = lambda: SignalStatusResponse(  # type: ignore[method-assign]
        state=SignalEngineState.WAITING_FOR_SCANNER,
        scanner_state="OFF",
        active_signal_count=0,
        watch_signal_count=0,
        terminal_signal_count=0,
        updated_at=None,
        latest_scanner_run_at=None,
        summary=SignalSummary(),
    )
    service = RiskService(stub, Settings(_env_file=None, environment="test"))  # type: ignore[arg-type]

    status = service.status()
    assert status.state is RiskEngineState.WAITING_FOR_SIGNALS
    assert status.summary.approved == 0
