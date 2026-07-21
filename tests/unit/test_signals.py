"""Signal Engine unit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.schemas.scanner import (
    CandidateLifecycle,
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
from app.schemas.signals import SignalEngineState, SignalLifecycle
from app.services.signals import SignalService

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class StubScanner:
    def __init__(self) -> None:
        self._candidates = [
            ScannerCandidate(
                candidate_id="q" * 64,
                symbol="BTCUSDT",
                direction=ScannerDirection.LONG,
                setup=ScannerSetup.TREND_PULLBACK,
                setup_name="Trend Pullback",
                reference_close_time=NOW,
                setup_confirmed_at=NOW,
                expires_at=NOW,
                qualification_expires_at=NOW,
                lifecycle=CandidateLifecycle.QUALIFIED,
                grade=ScannerGrade.A_PLUS,
                score=92,
                confidence=80,
                entry_ready=True,
                universe_rank=1,
                quote_volume=Decimal("1000000000"),
                spread_bps=Decimal("1"),
                entry_trigger_price=Decimal("101"),
                evaluated_at=NOW,
            ),
            ScannerCandidate(
                candidate_id="w" * 64,
                symbol="ETHUSDT",
                direction=ScannerDirection.SHORT,
                setup=ScannerSetup.EMA_REJECTION,
                setup_name="EMA Rejection",
                reference_close_time=NOW,
                setup_confirmed_at=NOW,
                expires_at=NOW,
                lifecycle=CandidateLifecycle.WATCH_NEAR,
                grade=ScannerGrade.B_PLUS,
                score=84,
                confidence=68,
                entry_ready=False,
                universe_rank=2,
                quote_volume=Decimal("900000000"),
                spread_bps=Decimal("2"),
                entry_trigger_price=Decimal("99"),
                evaluated_at=NOW,
            ),
            ScannerCandidate(
                candidate_id="e" * 64,
                symbol="SOLUSDT",
                direction=ScannerDirection.LONG,
                setup=ScannerSetup.CONTINUATION_SETUP,
                setup_name="Continuation Setup",
                reference_close_time=NOW,
                setup_confirmed_at=NOW,
                expires_at=NOW,
                lifecycle=CandidateLifecycle.EXPIRED,
                grade=ScannerGrade.A,
                score=88,
                confidence=75,
                entry_ready=False,
                universe_rank=3,
                quote_volume=Decimal("800000000"),
                spread_bps=Decimal("3"),
                entry_trigger_price=Decimal("150"),
                evaluated_at=NOW,
            ),
        ]

    def status(self) -> ScannerStatusResponse:
        return ScannerStatusResponse(
            state=ScannerState.ON,
            active_candidate_count=2,
            terminal_candidate_count=1,
            latest_run=ScannerRunSummary(
                run_id="run-1",
                run_type=ScannerRunType.FULL_UNIVERSE_SCAN,
                status=ScannerRunStatus.COMPLETED,
                run_started_at=NOW,
                completed_at=NOW,
            ),
        )

    def candidates(self) -> list[ScannerCandidate]:
        return self._candidates


def test_signal_service_projects_scanner_candidates() -> None:
    service = SignalService(StubScanner())  # type: ignore[arg-type]

    signals = service.signals()
    assert signals.count == 3
    assert signals.signals[0].lifecycle is SignalLifecycle.ACTIVE
    assert signals.signals[1].lifecycle is SignalLifecycle.WATCH
    assert signals.signals[2].lifecycle is SignalLifecycle.EXPIRED

    status = service.status()
    assert status.state is SignalEngineState.READY
    assert status.active_signal_count == 1
    assert status.watch_signal_count == 1
    assert status.terminal_signal_count == 1
    assert status.summary.a_plus_signals == 1
    assert status.summary.b_plus_watch == 1
    assert status.summary.expired == 1


def test_signal_service_waits_for_scanner_when_empty() -> None:
    stub = StubScanner()
    stub._candidates = []
    stub.status = lambda: ScannerStatusResponse(  # type: ignore[method-assign]
        state=ScannerState.OFF,
        active_candidate_count=0,
        terminal_candidate_count=0,
        latest_run=None,
    )
    service = SignalService(stub)  # type: ignore[arg-type]

    status = service.status()
    assert status.state is SignalEngineState.WAITING_FOR_SCANNER
    assert status.summary.active_signals == 0
