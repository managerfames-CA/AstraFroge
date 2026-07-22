"""Signal registry identity, lifecycle, retention, and API regression tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.api.v1.dependencies import get_signal_service
from app.main import create_app
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
from app.schemas.signals import SignalLifecycle
from app.services.execution import DemoExecutionService
from app.services.risk import RiskService
from app.services.signals import SignalService

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


def _candidate(
    key: str = "a",
    *,
    lifecycle: CandidateLifecycle = CandidateLifecycle.QUALIFIED,
    evaluated_at: datetime = NOW,
    score: int = 90,
    source_run_id: object = "scanner-run-1",
) -> ScannerCandidate:
    return ScannerCandidate(
        candidate_id=key * 64,
        symbol=f"{key.upper()}USDT",
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        reference_close_time=evaluated_at - timedelta(minutes=15),
        setup_confirmed_at=evaluated_at - timedelta(minutes=15),
        expires_at=evaluated_at + timedelta(minutes=45),
        qualification_expires_at=(
            evaluated_at + timedelta(minutes=15)
            if lifecycle is CandidateLifecycle.QUALIFIED
            else None
        ),
        lifecycle=lifecycle,
        score=score,
        confidence=80,
        grade=ScannerGrade.A_PLUS,
        entry_ready=lifecycle is CandidateLifecycle.QUALIFIED,
        universe_rank=1,
        quote_volume=Decimal("100000000"),
        spread_bps=Decimal("1"),
        entry_trigger_price=Decimal("100"),
        evaluated_at=evaluated_at,
        accepted_reasons=["Approved deterministic setup"],
        evidence={"source_run_id": source_run_id},
    )


def _run(
    status: ScannerRunStatus,
    at: datetime,
) -> ScannerRunSummary:
    return ScannerRunSummary(
        run_id=f"scanner-run-{status.value.lower()}",
        run_type=ScannerRunType.FULL_UNIVERSE_SCAN,
        status=status,
        run_started_at=at,
        completed_at=None if status is ScannerRunStatus.RUNNING else at,
    )


class StubScanner:
    def __init__(self, candidates: list[ScannerCandidate]) -> None:
        self.items = candidates
        self.state = ScannerState.ON
        self.latest_run: ScannerRunSummary | None = None

    def candidates(self) -> list[ScannerCandidate]:
        return self.items

    def status(self) -> ScannerStatusResponse:
        return ScannerStatusResponse(
            state=self.state,
            active_candidate_count=sum(
                item.lifecycle
                in {
                    CandidateLifecycle.DETECTED,
                    CandidateLifecycle.WATCH_NEAR,
                    CandidateLifecycle.QUALIFIED,
                }
                for item in self.items
            ),
            terminal_candidate_count=sum(
                item.lifecycle
                in {
                    CandidateLifecycle.EXPIRED,
                    CandidateLifecycle.INVALIDATED,
                    CandidateLifecycle.REJECTED,
                }
                for item in self.items
            ),
            latest_run=self.latest_run,
        )


def _service(scanner: StubScanner, *, record_limit: int = 1_000) -> SignalService:
    return SignalService(scanner, record_limit=record_limit)  # type: ignore[arg-type]


def test_signal_record_limit_must_be_positive() -> None:
    scanner = StubScanner([])

    with pytest.raises(ValueError, match="record limit must be positive"):
        _service(scanner, record_limit=0)


def test_signal_identity_is_independent_stable_and_duplicate_safe() -> None:
    candidate = _candidate()
    scanner = StubScanner([candidate, candidate])
    service = _service(scanner)

    first = service.signals()
    second = service.signals()

    assert first.count == 1
    assert second.count == 1
    record = first.signals[0]
    assert record.signal_id != candidate.candidate_id
    assert len(record.signal_id) == 64
    assert set(record.signal_id) <= set("0123456789abcdef")
    assert record.signal_id == second.signals[0].signal_id
    assert record.version == 1
    assert record.created_at == NOW
    assert record.updated_at == NOW
    assert record.source_run_id == "scanner-run-1"
    assert [item.lifecycle for item in record.lifecycle_history] == [SignalLifecycle.ACTIVE]


def test_non_lifecycle_update_increments_version_once_without_history_entry() -> None:
    scanner = StubScanner([_candidate()])
    service = _service(scanner)
    original = service.signals().signals[0]

    updated_at = NOW + timedelta(minutes=1)
    scanner.items = [_candidate(evaluated_at=updated_at, score=91)]
    updated = service.signals().signals[0]
    repeated = service.signals().signals[0]

    assert updated.signal_id == original.signal_id
    assert updated.version == 2
    assert updated.score == 91
    assert updated.updated_at == updated_at
    assert len(updated.lifecycle_history) == 1
    assert updated.lifecycle_history[0].sequence == 1
    assert repeated.version == 2


def test_signal_lifecycle_is_versioned_and_terminal_cannot_reactivate() -> None:
    scanner = StubScanner([_candidate()])
    service = _service(scanner)
    active = service.signals().signals[0]

    expired_at = NOW + timedelta(minutes=20)
    scanner.items = [
        _candidate(
            lifecycle=CandidateLifecycle.EXPIRED,
            evaluated_at=expired_at,
        )
    ]
    scanner.latest_run = _run(ScannerRunStatus.COMPLETED, expired_at)
    expired = service.signals().signals[0]

    assert expired.signal_id == active.signal_id
    assert expired.version == 2
    assert expired.created_at == active.created_at
    assert expired.updated_at == expired_at
    assert expired.terminal_at == expired_at
    assert [item.sequence for item in expired.lifecycle_history] == [1, 2]
    assert [item.lifecycle for item in expired.lifecycle_history] == [
        SignalLifecycle.ACTIVE,
        SignalLifecycle.EXPIRED,
    ]

    scanner.items = [_candidate(evaluated_at=expired_at + timedelta(minutes=5))]
    preserved = service.signals().signals[0]
    assert preserved.lifecycle is SignalLifecycle.EXPIRED
    assert preserved.version == 2


def test_missing_candidate_waits_for_successful_completed_scanner_run() -> None:
    scanner = StubScanner([_candidate()])
    service = _service(scanner)
    signal_id = service.signals().signals[0].signal_id
    scanner.items = []

    without_run = service.get(signal_id)
    assert without_run is not None
    assert without_run.lifecycle is SignalLifecycle.ACTIVE

    scanner.latest_run = _run(ScannerRunStatus.RUNNING, NOW + timedelta(minutes=1))
    during_run = service.get(signal_id)
    assert during_run is not None
    assert during_run.lifecycle is SignalLifecycle.ACTIVE

    scanner.latest_run = _run(ScannerRunStatus.FAILED, NOW + timedelta(minutes=2))
    after_failed_run = service.get(signal_id)
    assert after_failed_run is not None
    assert after_failed_run.lifecycle is SignalLifecycle.ACTIVE

    completed_at = NOW + timedelta(minutes=3)
    scanner.latest_run = _run(ScannerRunStatus.COMPLETED, completed_at)
    invalidated = service.get(signal_id)
    assert invalidated is not None
    assert invalidated.lifecycle is SignalLifecycle.INVALIDATED
    assert invalidated.version == 2
    assert invalidated.updated_at == completed_at
    assert invalidated.terminal_at == completed_at
    assert invalidated.lifecycle_history[-1].sequence == 2
    assert invalidated.lifecycle_history[-1].reason == "SOURCE_CANDIDATE_MISSING"
    assert "SOURCE_CANDIDATE_MISSING" in invalidated.audit_codes


def test_risk_block_transition_is_terminal_and_auditable() -> None:
    scanner = StubScanner([_candidate()])
    service = _service(scanner)
    signal_id = service.signals().signals[0].signal_id
    blocked_at = NOW + timedelta(minutes=1)

    blocked = service.mark_risk_blocked(
        signal_id,
        reason="MAX_OPEN_TRADES_REACHED",
        changed_at=blocked_at,
    )

    assert blocked is not None
    assert blocked.lifecycle is SignalLifecycle.RISK_BLOCKED
    assert blocked.version == 2
    assert blocked.updated_at == blocked_at
    assert blocked.terminal_at == blocked_at
    assert blocked.lifecycle_history[-1].sequence == 2
    assert blocked.lifecycle_history[-1].lifecycle is SignalLifecycle.RISK_BLOCKED
    assert blocked.lifecycle_history[-1].reason == "MAX_OPEN_TRADES_REACHED"
    assert blocked.audit_codes[-1] == "MAX_OPEN_TRADES_REACHED"
    assert service.signals().signals[0].lifecycle is SignalLifecycle.RISK_BLOCKED
    assert service.mark_risk_blocked(signal_id, reason="REPEAT") == blocked
    assert service.mark_risk_blocked("0" * 64, reason="UNKNOWN") is None
    with pytest.raises(ValueError, match="reason is required"):
        service.mark_risk_blocked(signal_id, reason="   ")


def test_signal_terminal_retention_is_bounded() -> None:
    scanner = StubScanner(
        [
            _candidate(
                key=key,
                lifecycle=CandidateLifecycle.EXPIRED,
                evaluated_at=NOW + timedelta(minutes=index),
            )
            for index, key in enumerate(("a", "b", "c"))
        ]
    )
    service = _service(scanner, record_limit=2)

    records = service.signals()

    assert records.count == 2
    assert {record.candidate_id for record in records.signals} == {
        "b" * 64,
        "c" * 64,
    }


def test_signal_active_overflow_is_hard_bounded() -> None:
    scanner = StubScanner(
        [
            _candidate(key=key, evaluated_at=NOW + timedelta(minutes=index))
            for index, key in enumerate(("a", "b", "c"))
        ]
    )
    service = _service(scanner, record_limit=2)

    records = service.signals()

    assert records.count == 2
    assert len(service._records_by_candidate) == 2
    assert len(service._candidate_by_signal) == 2
    assert {record.candidate_id for record in records.signals} == {
        "b" * 64,
        "c" * 64,
    }


def test_signal_retention_prunes_terminal_then_watch_before_active() -> None:
    scanner = StubScanner(
        [
            _candidate(
                key="a",
                lifecycle=CandidateLifecycle.EXPIRED,
                evaluated_at=NOW,
            ),
            _candidate(
                key="b",
                lifecycle=CandidateLifecycle.WATCH_NEAR,
                evaluated_at=NOW + timedelta(minutes=1),
            ),
            _candidate(key="c", evaluated_at=NOW + timedelta(minutes=2)),
            _candidate(key="d", evaluated_at=NOW + timedelta(minutes=3)),
        ]
    )
    service = _service(scanner, record_limit=2)

    records = service.signals()

    assert records.count == 2
    assert {record.candidate_id for record in records.signals} == {
        "c" * 64,
        "d" * 64,
    }
    assert all(record.lifecycle is SignalLifecycle.ACTIVE for record in records.signals)


def test_expanded_signal_record_remains_risk_and_execution_compatible(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    scanner = StubScanner([_candidate()])
    signal_service = _service(scanner)
    signal = signal_service.signals().signals[0]

    risk_service = RiskService(signal_service, settings)
    assessment = risk_service.assessments().assessments[0]
    execution_service = DemoExecutionService(risk_service, settings)
    plan = execution_service.plans().plans[0]

    assert assessment.signal_id == signal.signal_id
    assert assessment.signal_lifecycle is SignalLifecycle.ACTIVE
    assert plan.signal_id == signal.signal_id
    assert plan.signal_lifecycle is SignalLifecycle.ACTIVE


def test_signal_status_and_detail_api(
    settings,  # type: ignore[no-untyped-def]
) -> None:
    scanner = StubScanner([_candidate()])
    service = _service(scanner)
    status = service.status()
    signal = service.signals().signals[0]

    assert status.active_signal_count == 1
    assert status.watch_signal_count == 0
    assert status.terminal_signal_count == 0
    assert status.summary.a_plus_signals == 1

    app = create_app(settings)
    app.dependency_overrides[get_signal_service] = lambda: service
    with TestClient(app) as client:
        listing = client.get("/api/v1/signals")
        detail = client.get(f"/api/v1/signals/{signal.signal_id}")
        missing = client.get(f"/api/v1/signals/{'0' * 64}")
        invalid = client.get("/api/v1/signals/not-a-signal")

    assert listing.status_code == 200
    listing_body = listing.json()
    assert listing_body["count"] == 1
    assert listing_body["signals"][0]["signal_id"] == signal.signal_id

    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["signal_id"] == signal.signal_id
    assert detail_body["candidate_id"] == signal.candidate_id
    assert detail_body["lifecycle"] == "ACTIVE"
    assert detail_body["version"] == 1
    assert detail_body["created_at"] is not None
    assert detail_body["updated_at"] is not None
    assert detail_body["terminal_at"] is None
    assert detail_body["source_run_id"] == "scanner-run-1"
    assert detail_body["lifecycle_history"] == [
        {
            "sequence": 1,
            "lifecycle": "ACTIVE",
            "changed_at": detail_body["created_at"],
            "reason": "SCANNER_QUALIFIED",
        }
    ]
    assert missing.status_code == 404
    assert invalid.status_code == 422
