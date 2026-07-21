"""Regression coverage for the existing opportunity-preserving Scanner policy."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.schemas.scanner import CandidateLifecycle, ScannerDirection, ScannerGrade, ScannerSetup
from app.services.scanner_opportunity import OpportunityScannerEngine, OpportunityScannerService
from tests.unit.scanner_test_support import NOW, _prepare_setup, frame


def _weak_volume_context(direction: ScannerDirection, setup: ScannerSetup):  # type: ignore[no-untyped-def]
    ctx = _prepare_setup(direction, setup)
    if setup is ScannerSetup.TREND_PULLBACK:
        if direction is ScannerDirection.LONG:
            ctx.s[0] = frame(
                "102",
                open_="100",
                high="102.2",
                low="99.8",
                ema20="100",
                ema50="99",
                rsi="55",
                histogram="0.2",
                volume_ratio="0.5",
            )
        else:
            ctx.s[0] = frame(
                "98",
                open_="100",
                high="100.2",
                low="97.8",
                ema20="100",
                ema50="101",
                rsi="45",
                histogram="-0.2",
                volume_ratio="0.5",
            )
    elif direction is ScannerDirection.LONG:
        ctx.s[0] = frame(
            "100.1",
            open_="100",
            high="100.15",
            low="99.8",
            ema20="100",
            ema50="98",
            rsi="55",
            histogram="0.2",
            volume_ratio="0.5",
        )
    else:
        ctx.s[0] = frame(
            "99.9",
            open_="100",
            high="100.2",
            low="99.85",
            ema20="100",
            ema50="102",
            rsi="45",
            histogram="-0.2",
            volume_ratio="0.5",
        )
    return ctx


@pytest.mark.parametrize("direction", [ScannerDirection.LONG, ScannerDirection.SHORT])
def test_trend_pullback_keeps_structurally_valid_weak_volume_setup(
    direction: ScannerDirection,
) -> None:
    engine = OpportunityScannerEngine()
    ctx = _weak_volume_context(direction, ScannerSetup.TREND_PULLBACK)

    match = engine._trend_pullback(ctx)

    assert match.setup is ScannerSetup.TREND_PULLBACK
    assert match.evidence["soft_volume_warning"] is True
    assert match.evidence["setup_volume_ratio"] == Decimal("0.5")
    assert "pullback_swing_low" in match.evidence
    assert "pullback_swing_high" in match.evidence


@pytest.mark.parametrize("direction", [ScannerDirection.LONG, ScannerDirection.SHORT])
def test_ema_rejection_keeps_structurally_valid_weak_volume_setup(
    direction: ScannerDirection,
) -> None:
    engine = OpportunityScannerEngine()
    ctx = _weak_volume_context(direction, ScannerSetup.EMA_REJECTION)

    match = engine._ema_rejection(ctx)

    assert match.setup is ScannerSetup.EMA_REJECTION
    assert match.evidence["soft_volume_warning"] is True
    assert match.evidence["setup_volume_ratio"] == Decimal("0.5")


def _service() -> OpportunityScannerService:
    service = object.__new__(OpportunityScannerService)
    service._engine = OpportunityScannerEngine()
    return service


def test_soft_volume_rejected_candidate_is_preserved_as_watch() -> None:
    service = _service()
    ctx = _weak_volume_context(ScannerDirection.LONG, ScannerSetup.TREND_PULLBACK)
    match = service._engine._trend_pullback(ctx)
    service._engine.score = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
        79,
        59,
        ScannerGrade.REJECT,
        {"setup": Decimal("20")},
    )

    candidate = service._candidate_from_match(ctx, match, "run-1")

    assert candidate.lifecycle is CandidateLifecycle.WATCH_NEAR
    assert candidate.evidence["soft_volume_warning"] is True
    assert "VOLUME_BELOW_MINIMUM" in candidate.audit_codes


def test_soft_volume_watch_reasons_include_score_and_confidence_evidence() -> None:
    service = _service()
    ctx = _weak_volume_context(ScannerDirection.LONG, ScannerSetup.TREND_PULLBACK)
    match = service._engine._trend_pullback(ctx)
    service._engine.score = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
        79,
        59,
        ScannerGrade.REJECT,
        {"setup": Decimal("20")},
    )
    candidate = service._candidate_from_match(ctx, match, "run-2")
    candidate.audit_codes = []

    service._set_watch_reasons(candidate, ctx.e[0])

    assert "VOLUME_BELOW_MINIMUM" in candidate.audit_codes
    assert "CONFIDENCE_BELOW_60" in candidate.audit_codes
    assert "SCORE_BELOW_80" in candidate.audit_codes


@pytest.mark.parametrize("code", ["CONFIDENCE_BELOW_60", "SCORE_BELOW_80"])
def test_soft_volume_terminal_score_failures_remain_watch(code: str) -> None:
    service = _service()
    ctx = _weak_volume_context(ScannerDirection.LONG, ScannerSetup.TREND_PULLBACK)
    match = service._engine._trend_pullback(ctx)
    service._engine.score = lambda *_args, **_kwargs: (  # type: ignore[method-assign]
        79,
        59,
        ScannerGrade.REJECT,
        {"setup": Decimal("20")},
    )
    candidate = service._candidate_from_match(ctx, match, "run-3")
    candidate.audit_codes = []

    service._terminal(candidate, CandidateLifecycle.REJECTED, code, NOW)

    assert candidate.lifecycle is CandidateLifecycle.WATCH_NEAR
    assert "VOLUME_BELOW_MINIMUM" in candidate.audit_codes
    assert code in candidate.audit_codes
