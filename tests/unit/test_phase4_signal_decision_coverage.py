"""Direct branch coverage for the Phase 4 decision and fact-only Scanner boundaries."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

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
from app.schemas.signal_decision import (
    EntryTriggerStatus,
    SignalDecision,
    SignalDecisionStatus,
)
from app.schemas.signals import SignalLifecycle
from app.services.decision_signals import DecisionBackedSignalService
from app.services.scanner_base import ScannerEvaluationError, SetupMatch
from app.services.scanner_opportunity import OpportunityScannerService
from app.services.scanner_strategy_separated import StrategySeparatedScannerService
from app.services.signal_decision import SignalDecisionEngine
from app.services.strategy_evaluation import StrategyEvaluationResult

NOW = datetime(2026, 7, 19, 9, 0, tzinfo=UTC)


def _candidate(
    *,
    symbol: str = "BTCUSDT",
    candidate_id: str = "c" * 64,
    lifecycle: CandidateLifecycle = CandidateLifecycle.DETECTED,
    grade: ScannerGrade | None = ScannerGrade.A_PLUS,
    score: int | None = 92,
    confidence: int | None = 80,
    entry_ready: bool = True,
    trigger: Decimal = Decimal("100"),
    evidence: dict[str, Any] | None = None,
    audit_codes: list[str] | None = None,
) -> ScannerCandidate:
    payload = {
        "source_snapshot_version": "s" * 64,
        "entry_snapshot_close_time": (NOW - timedelta(minutes=1)).isoformat(),
        "strategy_reason_codes": [],
    }
    if evidence:
        payload.update(evidence)
    return ScannerCandidate(
        candidate_id=candidate_id,
        symbol=symbol,
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        reference_close_time=NOW - timedelta(minutes=15),
        setup_confirmed_at=NOW - timedelta(minutes=15),
        expires_at=NOW + timedelta(minutes=45),
        lifecycle=lifecycle,
        score=score,
        confidence=confidence,
        grade=grade,
        entry_ready=entry_ready,
        universe_rank=1,
        quote_volume=Decimal("100000000"),
        spread_bps=Decimal("1"),
        entry_trigger_price=trigger,
        evaluated_at=NOW,
        audit_codes=list(audit_codes or []),
        evidence=payload,
    )


def _decision_payload() -> dict[str, Any]:
    return {
        "decision_key": "d" * 64,
        "symbol": "BTCUSDT",
        "direction": ScannerDirection.LONG,
        "setup": ScannerSetup.TREND_PULLBACK,
        "setup_name": "Trend Pullback",
        "decision_status": SignalDecisionStatus.READY,
        "grade": ScannerGrade.A_PLUS,
        "score": 92,
        "confidence": 80,
        "entry_trigger_status": EntryTriggerStatus.READY,
        "selected": True,
        "ready": True,
        "source_snapshot_version": "s" * 64,
        "evaluated_at": NOW,
        "expires_at": NOW + timedelta(minutes=15),
        "fresh": True,
    }


@pytest.mark.parametrize(
    ("updates", "match"),
    [
        ({"grade": ScannerGrade.B_PLUS}, "B+ decisions"),
        ({"grade": ScannerGrade.REJECT}, "Rejected grade"),
        ({"ready": False}, "READY decisions must"),
        ({"entry_trigger_status": EntryTriggerStatus.PENDING}, "ready trigger"),
        ({"grade": None}, "A+/A grade"),
        ({"rejection_reasons": ["BLOCKED"]}, "blocking reasons"),
        (
            {
                "decision_status": SignalDecisionStatus.NEAR_SETUP,
                "selected": False,
            },
            "Only READY decisions",
        ),
        (
            {
                "decision_status": SignalDecisionStatus.NEAR_SETUP,
                "selected": False,
                "ready": False,
                "rejection_reasons": ["BLOCKED"],
            },
            "cannot contain blocking",
        ),
        (
            {
                "decision_status": SignalDecisionStatus.REJECTED,
                "selected": False,
                "ready": False,
            },
            "require at least one",
        ),
    ],
)
def test_signal_decision_contract_rejects_conflicting_states(
    updates: dict[str, Any],
    match: str,
) -> None:
    payload = _decision_payload()
    payload.update(updates)
    with pytest.raises(ValidationError, match=re.escape(match)):
        SignalDecision(**payload)


def test_decision_engine_maps_terminal_and_audit_reason_paths() -> None:
    engine = SignalDecisionEngine()
    invalidated = engine.decide(
        _candidate(
            lifecycle=CandidateLifecycle.INVALIDATED,
            audit_codes=["MISSING_5M_CANDLES", "CUSTOM_STRATEGY_REASON"],
        )
    )
    expired = engine.decide(_candidate(lifecycle=CandidateLifecycle.EXPIRED))
    rejected = engine.decide(_candidate(lifecycle=CandidateLifecycle.REJECTED))

    assert "CANDIDATE_INVALIDATED" in invalidated.rejection_reasons
    assert "MISSING_5M_CANDLES" in invalidated.rejection_reasons
    assert "CUSTOM_STRATEGY_REASON" in invalidated.strategy_reasons
    assert "CANDIDATE_EXPIRED" in expired.rejection_reasons
    assert "SETUP_INVALIDATED" in rejected.rejection_reasons


def test_decision_engine_handles_invalid_optional_metadata_and_incomplete_facts() -> None:
    engine = SignalDecisionEngine()
    invalid = engine.decide(
        _candidate(
            grade=None,
            score=None,
            confidence=None,
            trigger=Decimal("0"),
            evidence={
                "risk_reward": "not-a-number",
                "entry_snapshot_close_time": "not-a-date",
                "strategy_reason_codes": "not-a-list",
            },
        )
    )
    valid_rr = engine.decide(
        _candidate(
            entry_ready=False,
            confidence=65,
            evidence={"risk_reward": "2.5", "entry_snapshot_close_time": None},
        )
    )

    assert invalid.decision_status is SignalDecisionStatus.REJECTED
    assert "CONFLICTING_STRATEGY_FIELDS" in invalid.rejection_reasons
    assert "ENTRY_TRIGGER_INVALID" in invalid.rejection_reasons
    assert invalid.risk_reward is None
    assert valid_rr.risk_reward == Decimal("2.5")
    assert "CONFIDENCE_WATCH_ONLY" in valid_rr.watch_reasons
    assert valid_rr.expires_at == NOW + timedelta(minutes=45)


def test_decision_engine_preserves_unique_strategy_and_watch_codes() -> None:
    decision = SignalDecisionEngine().decide(
        _candidate(
            grade=ScannerGrade.B_PLUS,
            score=83,
            confidence=68,
            entry_ready=False,
            audit_codes=[
                "ENTRY_NOT_READY",
                "ENTRY_NOT_READY",
                "VOLUME_BELOW_MINIMUM",
                "UNCLASSIFIED_SETUP_FACT",
            ],
            evidence={"strategy_reason_codes": ["BREAKOUT_NOT_CONFIRMED"] * 2},
        )
    )

    assert decision.watch_reasons.count("ENTRY_NOT_READY") == 1
    assert "VOLUME_BELOW_MINIMUM" in decision.watch_reasons
    assert decision.strategy_reasons == [
        "BREAKOUT_NOT_CONFIRMED",
        "UNCLASSIFIED_SETUP_FACT",
    ]


class _ScannerStub:
    def __init__(self, candidates: list[ScannerCandidate]) -> None:
        self.items = candidates
        self.completed_at = NOW

    def candidates(self) -> list[ScannerCandidate]:
        return self.items

    def status(self) -> ScannerStatusResponse:
        return ScannerStatusResponse(
            state=ScannerState.ON,
            active_candidate_count=len(self.items),
            terminal_candidate_count=0,
            latest_run=ScannerRunSummary(
                run_id="coverage-run",
                run_type=ScannerRunType.ACTIVE_CANDIDATE_REFRESH,
                status=ScannerRunStatus.COMPLETED,
                run_started_at=self.completed_at,
                completed_at=self.completed_at,
            ),
        )

    def risk_stop_price(self, candidate_id: str) -> Decimal:
        return Decimal("95")


def test_decision_signal_get_risk_block_and_missing_paths() -> None:
    scanner = _ScannerStub([_candidate()])
    service = DecisionBackedSignalService(  # type: ignore[arg-type]
        scanner,
        SignalDecisionEngine(),
    )
    record = service.signals().signals[0]

    assert service.get("missing") is None
    with pytest.raises(ValueError, match="required"):
        service.mark_risk_blocked(record.signal_id, reason=" ")
    blocked = service.mark_risk_blocked(
        record.signal_id,
        reason="MANUAL_RISK_BLOCK",
        changed_at=NOW + timedelta(seconds=1),
    )
    assert blocked is not None
    assert blocked.lifecycle is SignalLifecycle.RISK_BLOCKED
    assert service.mark_risk_blocked(record.signal_id, reason="again") is blocked


def test_decision_signal_updates_same_key_and_prunes_terminal_records() -> None:
    first = _candidate(symbol="BTCUSDT", candidate_id="a" * 64)
    scanner = _ScannerStub([first])
    service = DecisionBackedSignalService(  # type: ignore[arg-type]
        scanner,
        SignalDecisionEngine(),
        record_limit=1,
    )
    original = service.signals().signals[0]

    first.confidence = 75
    first.evaluated_at = NOW + timedelta(seconds=1)
    scanner.completed_at = first.evaluated_at
    updated = service.signals().signals[0]
    assert updated.signal_id == original.signal_id
    assert updated.version == original.version + 1

    second = _candidate(
        symbol="ETHUSDT",
        candidate_id="b" * 64,
        evidence={"source_snapshot_version": "e" * 64},
    )
    scanner.items = [second]
    scanner.completed_at = NOW + timedelta(minutes=5)
    records = service.signals().signals
    assert len(records) == 1
    assert records[0].symbol == "ETHUSDT"


class _MarketFake:
    async def candles(self, symbol: str, interval: str, limit: int) -> Any:
        return SimpleNamespace(
            snapshot_version=f"{interval}-candle-version",
            candles=[object()] * limit,
        )


class _IndicatorFake:
    async def build(self, symbol: str, interval: str, limit: int) -> Any:
        return SimpleNamespace(
            snapshot_version=f"{interval}-indicator-version",
            points=[object()] * limit,
            structure=SimpleNamespace(state="bullish"),
        )


class _EngineFake:
    def align(self, candles: Any, indicators: Any, *, exchange_time: datetime) -> Any:
        frame = SimpleNamespace(candle=SimpleNamespace(close_time=NOW - timedelta(minutes=1)))
        return [frame], Decimal("1")

    def regime(self, frames: Any, structure: str) -> ScannerDirection:
        return ScannerDirection.LONG

    def volatility(self, frame: Any, interval: str) -> None:
        return None


@pytest.mark.anyio
async def test_fact_only_scanner_loads_context_and_refresh_provenance() -> None:
    service = object.__new__(StrategySeparatedScannerService)
    service._market = cast(Any, _MarketFake())
    service._indicators = cast(Any, _IndicatorFake())
    service._engine = cast(Any, _EngineFake())
    service._context_provenance = {}
    service._refresh_provenance = {}
    universe = cast(
        Any,
        SimpleNamespace(
            symbol="BTCUSDT",
            rank=1,
            quote_volume=Decimal("1"),
            spread_bps=Decimal("1"),
        ),
    )

    context = await service._load_context(universe, NOW)
    refreshed = await service._load_refresh_inputs("BTCUSDT", NOW)

    context_provenance = service._context_provenance[id(context)]
    assert len(context_provenance["source_snapshot_versions"]) == 6
    assert len(context_provenance["source_snapshot_version"]) == 64
    assert len(refreshed[0]) == 1
    assert "source_snapshot_version" in service._refresh_provenance["BTCUSDT"]


class _EvaluationFake:
    def __init__(self, result: StrategyEvaluationResult) -> None:
        self.result = result

    def evaluate(self, context: Any) -> StrategyEvaluationResult:
        return self.result


@pytest.mark.anyio
async def test_fact_only_scanner_evaluate_symbol_preserves_failures_and_selects_match() -> None:
    service = object.__new__(StrategySeparatedScannerService)
    context = cast(Any, SimpleNamespace(direction=ScannerDirection.LONG))

    async def load_context(universe: Any, exchange_time: datetime) -> Any:
        return context

    service._load_context = load_context  # type: ignore[method-assign]
    failure = ScannerEvaluationError(
        "BREAKOUT_NOT_CONFIRMED",
        "Breakout is pending",
        "15m",
    )
    service._strategy_evaluation = _EvaluationFake(
        StrategyEvaluationResult(matches=(), failures=(failure,))
    )
    universe = cast(Any, SimpleNamespace(symbol="BTCUSDT"))

    candidate, audits, returned_context = await service._evaluate_symbol(
        universe,
        NOW,
        "run-1",
    )
    assert candidate is None
    assert returned_context is context
    assert [item.code for item in audits] == [
        "BREAKOUT_NOT_CONFIRMED",
        "SETUP_NOT_DETECTED",
    ]

    match = cast(SetupMatch, object())
    high = _candidate(symbol="BTCUSDT", score=92, confidence=80)
    low = _candidate(
        symbol="ETHUSDT",
        candidate_id="e" * 64,
        score=87,
        confidence=75,
    )
    service._strategy_evaluation = _EvaluationFake(
        StrategyEvaluationResult(matches=(match, match), failures=(failure,))
    )
    generated = iter([low, high])
    service._candidate_from_match = (  # type: ignore[method-assign]
        lambda context, match, run_id: next(generated)
    )
    selected, audits, _ = await service._evaluate_symbol(universe, NOW, "run-2")

    assert selected is high
    assert selected.evidence["strategy_reason_codes"] == ["BREAKOUT_NOT_CONFIRMED"]
    assert any(item.code == "SUPERSEDED_BY_HIGHER_RANKED_SETUP" for item in audits)


def test_fact_only_scanner_candidate_normalization_and_helper_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(StrategySeparatedScannerService)
    context = cast(Any, object())
    candidate = _candidate(lifecycle=CandidateLifecycle.QUALIFIED)
    service._context_provenance = {
        id(context): {
            "source_snapshot_versions": {"5m:candles": "v"},
            "source_snapshot_version": "p" * 64,
            "entry_snapshot_close_time": NOW.isoformat(),
        }
    }
    monkeypatch.setattr(
        OpportunityScannerService,
        "_candidate_from_match",
        lambda self, context, match, run_id: candidate,
    )

    normalized = service._candidate_from_match(context, cast(SetupMatch, object()), "run")
    assert normalized.lifecycle is CandidateLifecycle.DETECTED
    assert normalized.qualification_expires_at is None
    assert normalized.evidence["legacy_scanner_lifecycle"] == "QUALIFIED"
    assert normalized.evidence["source_snapshot_version"] == "p" * 64

    complete_versions = {
        f"{interval}:{kind}": f"{interval}-{kind}"
        for interval in ("1h", "15m", "5m")
        for kind in ("candles", "indicators")
    }
    complete = service._provenance(complete_versions, NOW)
    incomplete = service._provenance({"5m:candles": "v"}, NOW)
    assert len(complete["source_snapshot_version"]) == 64
    assert "source_snapshot_version" not in incomplete

    target = _candidate()
    service._apply_provenance(target, complete)
    assert target.evidence["entry_snapshot_close_time"] == NOW.isoformat()
    assert service._candidate_order_key(_candidate(score=None, confidence=None))


def test_fact_only_scanner_terminal_keeps_score_failures_as_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(StrategySeparatedScannerService)
    candidate = _candidate()
    service._terminal(
        candidate,
        CandidateLifecycle.REJECTED,
        "SCORE_BELOW_80",
        NOW + timedelta(seconds=1),
    )
    assert candidate.lifecycle is CandidateLifecycle.DETECTED
    assert "SCORE_BELOW_80" in candidate.audit_codes

    delegated: list[str] = []
    monkeypatch.setattr(
        OpportunityScannerService,
        "_terminal",
        lambda self, candidate, state, code, evaluated_at: delegated.append(code),
    )
    service._terminal(
        candidate,
        CandidateLifecycle.INVALIDATED,
        "CANDIDATE_INVALIDATED",
        NOW,
    )
    assert delegated == ["CANDIDATE_INVALIDATED"]


@pytest.mark.anyio
async def test_fact_only_scanner_active_refresh_normalizes_legacy_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(StrategySeparatedScannerService)
    qualified = _candidate(lifecycle=CandidateLifecycle.QUALIFIED)
    qualified.qualification_expires_at = NOW + timedelta(minutes=5)
    watch = _candidate(
        symbol="ETHUSDT",
        candidate_id="e" * 64,
        lifecycle=CandidateLifecycle.WATCH_NEAR,
    )
    terminal = _candidate(
        symbol="SOLUSDT",
        candidate_id="x" * 64,
        lifecycle=CandidateLifecycle.EXPIRED,
    )
    service._candidates = {
        item.candidate_id: item for item in (qualified, watch, terminal)
    }
    service._refresh_provenance = {
        "BTCUSDT": {
            "source_snapshot_version": "z" * 64,
            "entry_snapshot_close_time": NOW.isoformat(),
        }
    }
    run = ScannerRunSummary(
        run_id="refresh",
        run_type=ScannerRunType.ACTIVE_CANDIDATE_REFRESH,
        status=ScannerRunStatus.COMPLETED,
        run_started_at=NOW,
        completed_at=NOW,
        qualified_candidates=2,
    )

    async def parent_refresh(self: Any) -> ScannerRunSummary:
        return run

    monkeypatch.setattr(OpportunityScannerService, "active_refresh", parent_refresh)
    result = await service.active_refresh()

    assert result.qualified_candidates == 0
    assert qualified.lifecycle is CandidateLifecycle.DETECTED
    assert watch.lifecycle is CandidateLifecycle.DETECTED
    assert terminal.lifecycle is CandidateLifecycle.EXPIRED
    assert qualified.qualification_expires_at is None
    assert qualified.evidence["source_snapshot_version"] == "z" * 64
