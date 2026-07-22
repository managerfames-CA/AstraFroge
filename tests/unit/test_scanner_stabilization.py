"""Regression tests for Scanner stabilization and bounded runtime state."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import MethodType
from typing import Any

import pytest

from app.schemas.scanner import (
    CandidateLifecycle,
    ScannerGrade,
    ScannerRunStatus,
    ScannerRunSummary,
    ScannerRunType,
    ScannerState,
)
from app.schemas.universe import UniverseCandidate, UniverseSnapshot
from app.services.scanner import ScannerService
from app.services.scanner_base import (
    EvaluationContext,
    ScannerEvaluationError,
    _candidate_key,
)
from app.services.scanner_contract import (
    MAX_SELECTED_CANDIDATES,
    SCANNER_RUN_HISTORY_LIMIT,
)
from app.services.scanner_runtime import (
    ScannerRuntimeBase,
    SystemScannerClock,
    next_five_minute_boundary,
)
from tests.unit.scanner_test_support import (
    NOW,
    FakeClock,
    FakeIndicators,
    FakeMarket,
    FakeUniverse,
    ScannerDirection,
    ScannerSetup,
    _candidate_for_service,
    _prepare_setup,
)


class LargeUniverse(FakeUniverse):
    """Return more eligible symbols than the final selection cap."""

    async def build(self) -> UniverseSnapshot:
        snapshot = await super().build()
        template = snapshot.candidates[0]
        candidates = [
            template.model_copy(
                update={
                    "rank": index + 1,
                    "symbol": f"S{index:03d}USDT",
                    "base_asset": f"S{index:03d}",
                }
            )
            for index in range(MAX_SELECTED_CANDIDATES + 10)
        ]
        return snapshot.model_copy(
            update={
                "max_symbols": len(candidates),
                "eligible_count": len(candidates),
                "candidates": candidates,
            }
        )


class StaleUniverse(FakeUniverse):
    """Return a Universe snapshot outside the Scanner freshness contract."""

    async def build(self) -> UniverseSnapshot:
        snapshot = await super().build()
        return snapshot.model_copy(update={"generated_at": NOW - timedelta(minutes=2)})


def _context_for(item: UniverseCandidate) -> EvaluationContext:
    return replace(
        _prepare_setup(ScannerDirection.LONG, ScannerSetup.TREND_PULLBACK),
        universe=item,
    )


def test_full_scan_evaluates_entire_universe_then_caps_selection() -> None:
    async def scenario() -> None:
        service = ScannerService(
            FakeMarket(),
            LargeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),
        )

        async def evaluate(
            self: ScannerService,
            item: UniverseCandidate,
            exchange_time: Any,
            run_id: str,
        ) -> tuple[Any, list[Any], EvaluationContext]:
            template = _candidate_for_service()
            candidate = template.model_copy(
                update={
                    "candidate_id": _candidate_key(
                        item.symbol,
                        ScannerDirection.LONG,
                        ScannerSetup.TREND_PULLBACK,
                        template.reference_close_time,
                    ),
                    "symbol": item.symbol,
                    "universe_rank": item.rank,
                    "evaluated_at": exchange_time,
                    "evidence": {"source_run_id": run_id},
                }
            )
            return candidate, [], _context_for(item)

        service._evaluate_symbol = MethodType(evaluate, service)  # type: ignore[method-assign]
        run = await service.run_now()

        assert run.status is ScannerRunStatus.COMPLETED
        assert run.universe_size == MAX_SELECTED_CANDIDATES + 10
        assert run.evaluated_symbols == MAX_SELECTED_CANDIDATES + 10
        assert run.successful_symbols == MAX_SELECTED_CANDIDATES + 10
        assert run.discovered_candidates == MAX_SELECTED_CANDIDATES + 10
        assert run.selected_candidates == MAX_SELECTED_CANDIDATES
        assert len(service.candidates()) == MAX_SELECTED_CANDIDATES

    asyncio.run(scenario())


def test_full_scan_stale_universe_and_symbol_failure_classification() -> None:
    async def scenario() -> None:
        stale = ScannerService(
            FakeMarket(),
            StaleUniverse(),
            FakeIndicators(),
            clock=FakeClock(),
        )
        stale_run = await stale.run_now()
        assert stale_run.status is ScannerRunStatus.FAILED
        assert stale_run.audits[0].code == "UNIVERSE_STALE"

        non_data = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),
        )

        async def reject_trend(
            self: ScannerService,
            item: UniverseCandidate,
            exchange_time: Any,
            run_id: str,
        ) -> tuple[Any, list[Any], EvaluationContext]:
            raise ScannerEvaluationError("TREND_SIDEWAYS", "range", "1h")

        non_data._evaluate_symbol = MethodType(reject_trend, non_data)  # type: ignore[method-assign]
        non_data_run = await non_data.run_now()
        assert non_data_run.status is ScannerRunStatus.COMPLETED
        assert non_data_run.successful_symbols == 1
        assert non_data_run.failed_symbols == 0
        assert non_data_run.audits[0].code == "TREND_SIDEWAYS"

        unexpected = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),
        )

        async def crash(
            self: ScannerService,
            item: UniverseCandidate,
            exchange_time: Any,
            run_id: str,
        ) -> tuple[Any, list[Any], EvaluationContext]:
            raise RuntimeError("unexpected")

        unexpected._evaluate_symbol = MethodType(crash, unexpected)  # type: ignore[method-assign]
        unexpected_run = await unexpected.run_now()
        assert unexpected_run.status is ScannerRunStatus.FAILED
        assert unexpected_run.failed_symbols == 1
        assert unexpected_run.audits[0].code == "INDICATOR_CALCULATION_FAILED"

    asyncio.run(scenario())


def test_full_scan_reentry_duplicate_and_terminal_selection_paths() -> None:
    async def scenario() -> None:
        template = _candidate_for_service()

        async def evaluate(
            self: ScannerService,
            item: UniverseCandidate,
            exchange_time: Any,
            run_id: str,
        ) -> tuple[Any, list[Any], EvaluationContext]:
            return (
                template.model_copy(update={"evaluated_at": exchange_time}),
                [],
                _context_for(item),
            )

        terminal_key = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),
        )
        terminal_key._terminal_keys.add(template.candidate_id)
        terminal_key._evaluate_symbol = MethodType(evaluate, terminal_key)  # type: ignore[method-assign]
        terminal_key_run = await terminal_key.run_now()
        assert terminal_key_run.selected_candidates == 0
        assert terminal_key_run.audits[-1].code == "REENTRY_COOLDOWN_ACTIVE"

        cooldown = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),
        )
        cooldown._terminal_history[
            (
                template.symbol,
                template.direction,
                template.setup,
            )
        ] = NOW
        cooldown._evaluate_symbol = MethodType(evaluate, cooldown)  # type: ignore[method-assign]
        cooldown_run = await cooldown.run_now()
        assert cooldown_run.selected_candidates == 0
        assert cooldown_run.audits[-1].code == "REENTRY_COOLDOWN_ACTIVE"

        qualified = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),
        )
        existing = _candidate_for_service(lifecycle=CandidateLifecycle.QUALIFIED)
        qualified._candidates[existing.candidate_id] = existing
        qualified._evaluate_symbol = MethodType(evaluate, qualified)  # type: ignore[method-assign]
        qualified_run = await qualified.run_now()
        retained = qualified._candidates[existing.candidate_id]
        assert qualified_run.updated_candidates == 1
        assert qualified_run.qualified_candidates == 1
        assert retained.lifecycle is CandidateLifecycle.QUALIFIED
        assert retained.entry_ready is True
        assert "DUPLICATE_CANDIDATE_UPDATED" in retained.audit_codes

        terminal = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),
        )
        rejected = template.model_copy(
            update={
                "lifecycle": CandidateLifecycle.REJECTED,
                "score": 79,
                "grade": ScannerGrade.REJECT,
                "audit_codes": ["SCORE_BELOW_80"],
            }
        )

        async def evaluate_rejected(
            self: ScannerService,
            item: UniverseCandidate,
            exchange_time: Any,
            run_id: str,
        ) -> tuple[Any, list[Any], EvaluationContext]:
            return rejected, [], _context_for(item)

        terminal._evaluate_symbol = MethodType(evaluate_rejected, terminal)  # type: ignore[method-assign]
        terminal_run = await terminal.run_now()
        assert terminal_run.selected_candidates == 0
        assert rejected.candidate_id in terminal._terminal_keys
        assert rejected.candidate_id not in terminal._candidate_contexts

    asyncio.run(scenario())


def test_run_history_is_bounded() -> None:
    service = ScannerService(
        FakeMarket(),
        FakeUniverse(),
        FakeIndicators(),
        clock=FakeClock(),
    )
    for index in range(SCANNER_RUN_HISTORY_LIMIT + 5):
        service._append_run(
            ScannerRunSummary(
                run_id=str(index),
                run_type=ScannerRunType.FULL_UNIVERSE_SCAN,
                status=ScannerRunStatus.COMPLETED,
                run_started_at=NOW,
                completed_at=NOW,
            )
        )

    assert len(service._runs) == SCANNER_RUN_HISTORY_LIMIT
    assert service._runs[0].run_id == "5"


def test_terminal_candidate_and_history_retention_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.scanner_runtime.SCANNER_TERMINAL_CANDIDATE_LIMIT",
        2,
    )
    monkeypatch.setattr(
        "app.services.scanner_runtime.SCANNER_TERMINAL_HISTORY_LIMIT",
        2,
    )
    service = ScannerService(
        FakeMarket(),
        FakeUniverse(),
        FakeIndicators(),
        clock=FakeClock(),
    )

    candidate_ids: list[str] = []
    for index in range(3):
        candidate = _candidate_for_service(
            lifecycle=CandidateLifecycle.EXPIRED,
        ).model_copy(
            update={
                "candidate_id": str(index) * 64,
                "symbol": f"T{index}USDT",
                "evaluated_at": NOW + timedelta(minutes=index),
            }
        )
        candidate_ids.append(candidate.candidate_id)
        service._candidates[candidate.candidate_id] = candidate
        service._record_terminal(candidate)

    assert len(service._candidates) == 2
    assert candidate_ids[0] not in service._candidates
    assert candidate_ids[0] in service._terminal_keys
    assert len(service._terminal_history) == 2


def test_pruned_terminal_payload_cannot_reactivate() -> None:
    async def scenario() -> None:
        service = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),
        )
        template = _candidate_for_service(lifecycle=CandidateLifecycle.EXPIRED)
        service._terminal_keys.add(template.candidate_id)
        service._candidates.pop(template.candidate_id, None)
        service._terminal_history.clear()

        async def evaluate(
            self: ScannerService,
            item: UniverseCandidate,
            exchange_time: Any,
            run_id: str,
        ) -> tuple[Any, list[Any], EvaluationContext]:
            return (
                template.model_copy(
                    update={
                        "lifecycle": CandidateLifecycle.DETECTED,
                        "evaluated_at": exchange_time,
                    }
                ),
                [],
                _context_for(item),
            )

        service._evaluate_symbol = MethodType(evaluate, service)  # type: ignore[method-assign]
        run = await service.run_now()

        assert run.selected_candidates == 0
        assert run.audits[-1].code == "REENTRY_COOLDOWN_ACTIVE"
        assert template.candidate_id not in service._candidates

    asyncio.run(scenario())


def test_start_schedules_immediate_scan_without_waiting_for_completion() -> None:
    async def scenario() -> None:
        service = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_scan(self: ScannerService) -> ScannerRunSummary:
            started.set()
            await release.wait()
            return ScannerRunSummary(
                run_id="slow",
                run_type=ScannerRunType.FULL_UNIVERSE_SCAN,
                status=ScannerRunStatus.COMPLETED,
                run_started_at=NOW,
                completed_at=NOW,
            )

        service.full_scan = MethodType(slow_scan, service)  # type: ignore[method-assign]
        status = await service.start()

        assert status.state is ScannerState.ON
        assert status.scheduler_running is True
        await asyncio.wait_for(started.wait(), timeout=1)
        assert release.is_set() is False

        repeated = await service.start()
        assert repeated.state is ScannerState.ON
        await service.stop()
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_runtime_clock_boundary_and_abstract_operations() -> None:
    async def scenario() -> None:
        clock = SystemScannerClock()
        assert clock.now().tzinfo is not None
        await clock.sleep(0)

        boundary = next_five_minute_boundary(NOW + timedelta(minutes=2, seconds=30))
        assert boundary == NOW + timedelta(minutes=5)

        runtime = ScannerRuntimeBase(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),
        )
        with pytest.raises(NotImplementedError):
            await runtime.full_scan()
        with pytest.raises(NotImplementedError):
            await runtime.active_refresh()

    asyncio.run(scenario())
