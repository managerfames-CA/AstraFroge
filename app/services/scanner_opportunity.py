"""Opportunity-preserving Scanner policy layered on the deterministic scanner runtime."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.schemas.scanner import CandidateLifecycle, ScannerCandidate, ScannerDirection, ScannerSetup
from app.services.scanner import ScannerService
from app.services.scanner_base import (
    D0,
    D1,
    EvaluationContext,
    Frame,
    ScannerEvaluationError,
    SetupMatch,
    _body,
    _directional_close_position,
    _directional_delta,
    _directional_extreme,
    _directional_reclaim_margin,
    _directional_wick,
    _frame_value,
    _n_down,
    _n_up,
)
from app.services.scanner_scoring import ScannerEngine


class OpportunityScannerEngine(ScannerEngine):
    """Keep pullback/EMA setups alive when only setup-volume is weak.

    Breakout Retest, Liquidity Sweep Reversal, and Continuation keep their existing
    hard volume confirmation rules. Trend Pullback and EMA Rejection keep the same
    structural formulas, but weak 15M volume becomes audit/scoring evidence instead
    of an immediate setup rejection.
    """

    def _trend_pullback(self, ctx: EvaluationContext) -> SetupMatch:
        s0, s1, s2, s3 = ctx.s[:4]
        direction = ctx.direction
        atr0 = _frame_value(s0, "atr14")
        atr1 = _frame_value(s1, "atr14")
        ema20_0 = _frame_value(s0, "ema20")
        ema20_1 = _frame_value(s1, "ema20")
        ema50_1 = _frame_value(s1, "ema50")
        rsi0 = _frame_value(s0, "rsi14")
        rsi1 = _frame_value(s1, "rsi14")
        hist0 = _frame_value(s0, "macd_histogram")
        hist1 = _frame_value(s1, "macd_histogram")
        sequence = (
            s3.candle.close > s2.candle.close > s1.candle.close
            if direction is ScannerDirection.LONG
            else s3.candle.close < s2.candle.close < s1.candle.close
        )
        if not sequence:
            raise ScannerEvaluationError(
                "PULLBACK_SEQUENCE_FAILED", "Three-candle pullback sequence failed", "15m"
            )
        if direction is ScannerDirection.LONG:
            zone = (
                s1.candle.low <= ema20_1
                and s1.candle.high >= ema50_1
                and s1.candle.close >= ema50_1 - Decimal("0.25") * atr1
            )
            recovery = (
                s0.candle.close > s0.candle.open
                and s0.candle.close > ema20_0
                and _directional_close_position(s0.candle, direction) >= Decimal("0.65")
                and Decimal("48") <= rsi0 <= Decimal("65")
                and rsi0 > rsi1
                and hist0 > hist1
            )
        else:
            zone = (
                s1.candle.high >= ema20_1
                and s1.candle.low <= ema50_1
                and s1.candle.close <= ema50_1 + Decimal("0.25") * atr1
            )
            recovery = (
                s0.candle.close < s0.candle.open
                and s0.candle.close < ema20_0
                and _directional_close_position(s0.candle, direction) >= Decimal("0.65")
                and Decimal("35") <= rsi0 <= Decimal("52")
                and rsi0 < rsi1
                and hist0 < hist1
            )
        if not zone:
            raise ScannerEvaluationError(
                "PULLBACK_ZONE_MISSED", "EMA20/EMA50 pullback zone was missed", "15m"
            )
        if not recovery:
            raise ScannerEvaluationError(
                "PULLBACK_SEQUENCE_FAILED", "Trend Pullback recovery candle failed", "15m"
            )
        zone_low = min(ema20_1, ema50_1)
        zone_high = max(ema20_1, ema50_1)
        if zone_low <= s1.candle.close <= zone_high:
            distance = D0
        else:
            distance = min(
                abs(s1.candle.close - zone_low),
                abs(s1.candle.close - zone_high),
            )
        points = (
            Decimal("8.75") * _n_down(distance / atr1, D0, Decimal("0.25"))
            + Decimal("6.25") * _n_up(_body(s0.candle) / atr0, Decimal("0.10"), D1)
            + Decimal("5")
            * _n_up(_directional_delta(rsi0, rsi1, direction), D0, Decimal("10"))
            + Decimal("5")
            * _n_up(
                _directional_delta(hist0, hist1, direction) / atr0,
                D0,
                Decimal("0.10"),
            )
        )
        evidence: dict[str, Any] = {
            "pullback_swing_low": min(item.candle.low for item in (s3, s2, s1, s0)),
            "pullback_swing_high": max(item.candle.high for item in (s3, s2, s1, s0)),
            "soft_volume_warning": _frame_value(s0, "volume_ratio") < Decimal("0.80"),
            "setup_volume_ratio": _frame_value(s0, "volume_ratio"),
        }
        trigger = self._entry_trigger(ScannerSetup.TREND_PULLBACK, ctx)
        return self._match(
            ScannerSetup.TREND_PULLBACK,
            ctx,
            s0.candle.close_time,
            trigger,
            points,
            evidence=evidence,
        )

    def _ema_rejection(self, ctx: EvaluationContext) -> SetupMatch:
        s0, s1 = ctx.s[:2]
        direction = ctx.direction
        atr = _frame_value(s0, "atr14")
        selected = self.selected_ema(s0, direction)
        body = _body(s0.candle)
        if body <= 0:
            raise ScannerEvaluationError(
                "INVALID_15M_OHLCV", "EMA rejection body is zero", "15m"
            )
        rsi = _frame_value(s0, "rsi14")
        hist0 = _frame_value(s0, "macd_histogram")
        hist1 = _frame_value(s1, "macd_histogram")
        distance = abs(_directional_extreme(s0.candle, direction) - selected)
        valid = (
            distance <= Decimal("0.20") * atr
            and _directional_reclaim_margin(s0.candle.close, selected, direction)
            >= Decimal("0.05") * atr
            and body >= Decimal("0.10") * atr
            and _directional_wick(s0.candle, direction) / body >= Decimal("1.50")
            and _directional_close_position(s0.candle, direction) >= Decimal("0.70")
            and (
                Decimal("45") <= rsi <= Decimal("65")
                if direction is ScannerDirection.LONG
                else Decimal("35") <= rsi <= Decimal("55")
            )
            and _directional_delta(hist0, hist1, direction) >= 0
        )
        if not valid:
            raise ScannerEvaluationError(
                "EMA_REJECTION_NOT_CONFIRMED", "EMA Rejection formula failed", "15m"
            )
        points = (
            Decimal("7.50")
            * _n_up(
                _directional_wick(s0.candle, direction) / body,
                Decimal("1.50"),
                Decimal("4"),
            )
            + Decimal("7.50") * _n_down(distance / atr, D0, Decimal("0.20"))
            + Decimal("6.25")
            * _n_up(
                _directional_close_position(s0.candle, direction),
                Decimal("0.70"),
                Decimal("0.90"),
            )
            + Decimal("3.75")
            * _n_up(_frame_value(s0, "volume_ratio"), D1, Decimal("2.50"))
        )
        trigger = self._entry_trigger(ScannerSetup.EMA_REJECTION, ctx)
        return self._match(
            ScannerSetup.EMA_REJECTION,
            ctx,
            s0.candle.close_time,
            trigger,
            points,
            selected_ema=selected,
            evidence={
                "soft_volume_warning": _frame_value(s0, "volume_ratio") < D1,
                "setup_volume_ratio": _frame_value(s0, "volume_ratio"),
            },
        )


class OpportunityScannerService(ScannerService):
    """Preserve weak-volume pullback/EMA matches as 5M-monitored watch candidates."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._engine = OpportunityScannerEngine()

    def _candidate_from_match(
        self,
        context: EvaluationContext,
        match: SetupMatch,
        run_id: str,
    ) -> ScannerCandidate:
        candidate = super()._candidate_from_match(context, match, run_id)
        if (
            candidate.lifecycle is CandidateLifecycle.REJECTED
            and match.evidence.get("soft_volume_warning") is True
        ):
            candidate.lifecycle = CandidateLifecycle.WATCH_NEAR
            if "VOLUME_BELOW_MINIMUM" not in candidate.audit_codes:
                candidate.audit_codes.insert(0, "VOLUME_BELOW_MINIMUM")
        return candidate

    def _set_watch_reasons(self, candidate: ScannerCandidate, e0: Frame) -> None:
        super()._set_watch_reasons(candidate, e0)
        if candidate.evidence.get("soft_volume_warning") is True:
            if "VOLUME_BELOW_MINIMUM" not in candidate.audit_codes:
                candidate.audit_codes.insert(0, "VOLUME_BELOW_MINIMUM")
            if candidate.confidence is not None and candidate.confidence < 60:
                if "CONFIDENCE_BELOW_60" not in candidate.audit_codes:
                    candidate.audit_codes.append("CONFIDENCE_BELOW_60")
            if candidate.score is not None and candidate.score < 80:
                if "SCORE_BELOW_80" not in candidate.audit_codes:
                    candidate.audit_codes.append("SCORE_BELOW_80")

    def _terminal(
        self,
        candidate: ScannerCandidate,
        state: CandidateLifecycle,
        code: str,
        evaluated_at: Any,
    ) -> None:
        if (
            candidate.evidence.get("soft_volume_warning") is True
            and code in {"CONFIDENCE_BELOW_60", "SCORE_BELOW_80"}
        ):
            candidate.lifecycle = CandidateLifecycle.WATCH_NEAR
            for audit_code in ("VOLUME_BELOW_MINIMUM", code):
                if audit_code not in candidate.audit_codes:
                    candidate.audit_codes.append(audit_code)
            candidate.evaluated_at = evaluated_at
            return
        super()._terminal(candidate, state, code, evaluated_at)
