"""Phase 4 Signal Decision Engine safety, integration, and persistence tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.persistence.database import Persistence
from app.persistence.repositories import TradingStateRepositories
from app.schemas.risk import RiskDecision
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
from app.schemas.signal_decision import EntryTriggerStatus, SignalDecisionStatus
from app.schemas.signals import SignalLifecycle
from app.services.decision_signals import (
    DecisionBackedSignalService,
    PersistentDecisionSignalService,
)
from app.services.execution import DemoExecutionService
from app.services.risk import RiskService
from app.services.signal_decision import SignalDecisionEngine

NOW = datetime(2026, 7, 19, 8, 0, tzinfo=UTC)


def _candidate(
    *,
    symbol: str = "BTCUSDT",
    grade: ScannerGrade = ScannerGrade.A_PLUS,
    score: int = 92,
    confidence: int = 80,
    entry_ready: bool = True,
    snapshot_version: str | None = "a" * 64,
    lifecycle: CandidateLifecycle = CandidateLifecycle.DETECTED,
    stale: bool = False,
    audit_codes: list[str] | None = None,
    evaluated_at: datetime = NOW,
    expires_at: datetime | None = None,
) -> ScannerCandidate:
    evidence: dict[str, Any] = {
        "entry_snapshot_close_time": (evaluated_at - timedelta(minutes=1)).isoformat(),
        "strategy_reason_codes": ["BREAKOUT_NOT_CONFIRMED"],
    }
    if snapshot_version is not None:
        evidence["source_snapshot_version"] = snapshot_version
    return ScannerCandidate(
        candidate_id=(symbol[0].lower() * 64),
        symbol=symbol,
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        reference_close_time=evaluated_at - timedelta(minutes=15),
        setup_confirmed_at=evaluated_at - timedelta(minutes=15),
        expires_at=expires_at or evaluated_at + timedelta(minutes=45),
        lifecycle=lifecycle,
        score=score,
        confidence=confidence,
        grade=grade,
        entry_ready=entry_ready,
        stale=stale,
        universe_rank=1,
        quote_volume=Decimal("100000000"),
        spread_bps=Decimal("1"),
        entry_trigger_price=Decimal("100"),
        evaluated_at=evaluated_at,
        accepted_reasons=["TREND_PULLBACK_CONFIRMED"],
        audit_codes=list(audit_codes or []),
        evidence=evidence,
    )


class StubScanner:
    def __init__(self, candidates: list[ScannerCandidate]) -> None:
        self._candidates = candidates
        self.completed_at = NOW

    def status(self) -> ScannerStatusResponse:
        return ScannerStatusResponse(
            state=ScannerState.ON,
            active_candidate_count=len(self._candidates),
            terminal_candidate_count=0,
            latest_run=ScannerRunSummary(
                run_id="phase4-run",
                run_type=ScannerRunType.ACTIVE_CANDIDATE_REFRESH,
                status=ScannerRunStatus.COMPLETED,
                run_started_at=self.completed_at,
                completed_at=self.completed_at,
            ),
        )

    def candidates(self) -> list[ScannerCandidate]:
        return self._candidates

    def risk_stop_price(self, candidate_id: str) -> Decimal:
        return Decimal("95")


class PrivateRiskFake:
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
            {"symbol": "BTCUSDT", "positionAmt": "0", "leverage": "3"},
            {"symbol": "ETHUSDT", "positionAmt": "0", "leverage": "3"},
        ]

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return []


class NeverOrderClient:
    def __init__(self) -> None:
        self.order_calls = 0

    def place_market_order(self, **kwargs: Any) -> dict[str, Any]:
        self.order_calls += 1
        raise AssertionError("Demo order submission must remain unreachable")


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        scanner_auto_start=False,
        execution_enabled=False,
        risk_per_trade_percent=Decimal("1"),
        risk_max_margin_exposure_usdt=Decimal("10000"),
        risk_max_open_trades=4,
    )


def test_a_plus_and_a_valid_setup_can_become_ready() -> None:
    engine = SignalDecisionEngine()
    for grade, score in ((ScannerGrade.A_PLUS, 92), (ScannerGrade.A, 87)):
        decision = engine.decide(_candidate(grade=grade, score=score))
        assert decision.decision_status is SignalDecisionStatus.READY
        assert decision.ready is True
        assert decision.selected is True
        assert decision.entry_trigger_status is EntryTriggerStatus.READY


def test_b_plus_and_pending_trigger_are_near_setup_only() -> None:
    engine = SignalDecisionEngine()
    b_plus = engine.decide(_candidate(grade=ScannerGrade.B_PLUS, score=83, confidence=75))
    pending = engine.decide(_candidate(entry_ready=False))

    assert b_plus.decision_status is SignalDecisionStatus.NEAR_SETUP
    assert b_plus.ready is False
    assert b_plus.selected is False
    assert "GRADE_B_PLUS_WATCH_ONLY" in b_plus.watch_reasons
    assert pending.decision_status is SignalDecisionStatus.NEAR_SETUP
    assert pending.entry_trigger_status is EntryTriggerStatus.PENDING
    assert "ENTRY_NOT_READY" in pending.watch_reasons


def test_rejected_grade_or_blocking_reason_cannot_be_ready() -> None:
    engine = SignalDecisionEngine()
    rejected_grade = engine.decide(_candidate(grade=ScannerGrade.REJECT, score=70, confidence=75))
    blocked = engine.decide(_candidate(audit_codes=["STALE_5M_DATA"]))

    assert rejected_grade.decision_status is SignalDecisionStatus.REJECTED
    assert rejected_grade.ready is False
    assert rejected_grade.selected is False
    assert "SCORE_BELOW_80" in rejected_grade.rejection_reasons
    assert blocked.decision_status is SignalDecisionStatus.REJECTED
    assert "STALE_5M_DATA" in blocked.rejection_reasons


def test_missing_stale_expired_and_conflicting_provenance_fail_closed() -> None:
    engine = SignalDecisionEngine()
    missing = engine.decide(_candidate(snapshot_version=None))
    stale = engine.decide(_candidate(stale=True))
    expired = engine.decide(_candidate(expires_at=NOW))
    conflicting = engine.decide(_candidate(grade=ScannerGrade.A, score=84))

    assert missing.decision_status is SignalDecisionStatus.REJECTED
    assert "MISSING_SOURCE_PROVENANCE" in missing.rejection_reasons
    assert stale.decision_status is SignalDecisionStatus.REJECTED
    assert "SOURCE_SNAPSHOT_STALE" in stale.rejection_reasons
    assert expired.decision_status is SignalDecisionStatus.REJECTED
    assert "SIGNAL_DECISION_EXPIRED" in expired.rejection_reasons
    assert conflicting.decision_status is SignalDecisionStatus.REJECTED
    assert "CONFLICTING_STRATEGY_FIELDS" in conflicting.rejection_reasons


def test_strategy_reasons_are_preserved_without_blocking_valid_selected_setup() -> None:
    decision = SignalDecisionEngine().decide(_candidate())

    assert decision.decision_status is SignalDecisionStatus.READY
    assert "BREAKOUT_NOT_CONFIRMED" in decision.strategy_reasons
    assert decision.rejection_reasons == []


def test_unchanged_decision_deduplicates_and_new_snapshot_creates_new_identity() -> None:
    candidate = _candidate()
    scanner = StubScanner([candidate])
    service = DecisionBackedSignalService(  # type: ignore[arg-type]
        scanner,
        SignalDecisionEngine(),
    )

    first = service.signals()
    repeated = service.signals()
    assert first.count == 1
    assert repeated.count == 1
    assert first.signals[0].signal_id == repeated.signals[0].signal_id

    candidate.evidence["source_snapshot_version"] = "b" * 64
    candidate.evidence["entry_snapshot_close_time"] = (NOW + timedelta(minutes=4)).isoformat()
    candidate.evaluated_at = NOW + timedelta(minutes=5)
    scanner.completed_at = candidate.evaluated_at
    advanced = service.signals()

    assert advanced.count == 2
    assert len({item.signal_id for item in advanced.signals}) == 2
    assert sum(item.lifecycle is SignalLifecycle.ACTIVE for item in advanced.signals) == 1
    assert sum(item.lifecycle is SignalLifecycle.INVALIDATED for item in advanced.signals) == 1


@pytest.fixture
def repositories(tmp_path: Path) -> Iterator[TradingStateRepositories]:
    persistence = Persistence(f"sqlite+pysqlite:///{tmp_path / 'phase4.db'}")
    persistence.initialize()
    repository = TradingStateRepositories(persistence)
    try:
        yield repository
    finally:
        persistence.close()


def test_persistent_restart_recovers_same_decision_without_duplicate(
    repositories: TradingStateRepositories,
) -> None:
    scanner = StubScanner([_candidate()])
    first_service = PersistentDecisionSignalService(  # type: ignore[arg-type]
        scanner,
        SignalDecisionEngine(),
        repositories,
    )
    first = first_service.signals()

    restarted = PersistentDecisionSignalService(  # type: ignore[arg-type]
        scanner,
        SignalDecisionEngine(),
        repositories,
    ).signals()

    assert first.count == 1
    assert restarted.count == 1
    assert restarted.signals[0].signal_id == first.signals[0].signal_id
    assert restarted.signals[0].decision_key == first.signals[0].decision_key


def test_risk_receives_only_ready_and_near_setup_never_reaches_execution() -> None:
    ready = _candidate(symbol="BTCUSDT")
    near = _candidate(
        symbol="ETHUSDT",
        grade=ScannerGrade.B_PLUS,
        score=83,
        confidence=75,
        entry_ready=True,
        snapshot_version="e" * 64,
    )
    scanner = StubScanner([ready, near])
    signals = DecisionBackedSignalService(  # type: ignore[arg-type]
        scanner,
        SignalDecisionEngine(),
    )
    risk = RiskService(
        signals,
        _settings(),
        PrivateRiskFake(),
        now_provider=lambda: NOW,
    )

    assessments = risk.assessments().assessments
    approved = [item for item in assessments if item.decision is RiskDecision.APPROVED]
    watch = [item for item in assessments if item.decision is RiskDecision.WATCH]

    assert len(approved) == 1
    assert approved[0].symbol == "BTCUSDT"
    assert approved[0].approved_for_execution is True
    assert len(watch) == 1
    assert watch[0].symbol == "ETHUSDT"
    assert watch[0].approved_for_execution is False

    client = NeverOrderClient()
    execution = DemoExecutionService(risk, _settings(), client)  # type: ignore[arg-type]
    assert execution.auto_execute_pending() == 0
    assert client.order_calls == 0


def test_execution_enabled_default_and_phase4_settings_remain_false() -> None:
    assert Settings(_env_file=None).execution_enabled is False
    assert _settings().execution_enabled is False
