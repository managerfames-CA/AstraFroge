"""Five deterministic Scanner Contract Version 1 setup evaluators."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from app.schemas.scanner import ScannerDirection, ScannerSetup
from app.services.scanner_base import (
    D0,
    D1,
    EvaluationContext,
    ScannerEngineBase,
    ScannerEvaluationError,
    SetupMatch,
    _body,
    _directional_break_margin,
    _directional_close_position,
    _directional_compression_boundary,
    _directional_delta,
    _directional_extreme,
    _directional_histogram,
    _directional_previous_break_level,
    _directional_reclaim_margin,
    _directional_sweep_depth,
    _directional_wick,
    _frame_value,
    _n_down,
    _n_up,
    _range,
)
from app.services.scanner_contract import EXPIRY_LIMITS, SETUP_NAMES


class ScannerSetupEngine(ScannerEngineBase):
    """Evaluate all five approved LONG and SHORT setups."""

    def evaluate_setups(
        self, ctx: EvaluationContext
    ) -> tuple[list[SetupMatch], list[ScannerEvaluationError]]:
        matches: list[SetupMatch] = []
        failures: list[ScannerEvaluationError] = []
        evaluators = (
            self._trend_pullback,
            self._breakout_retest,
            self._ema_rejection,
            self._liquidity_sweep,
            self._continuation,
        )
        for evaluator in evaluators:
            try:
                matches.append(evaluator(ctx))
            except ScannerEvaluationError as exc:
                failures.append(exc)
        return matches, failures

    def setups(self, ctx: EvaluationContext) -> list[SetupMatch]:
        matches, _ = self.evaluate_setups(ctx)
        return matches

    def _entry_trigger(
        self,
        setup: ScannerSetup,
        ctx: EvaluationContext,
        *,
        level: Decimal | None = None,
    ) -> Decimal:
        direction = ctx.direction
        e0, e1, e2 = ctx.e[0], ctx.e[1], ctx.e[2]
        atr = _frame_value(e0, "atr14")
        if setup is ScannerSetup.TREND_PULLBACK:
            if direction is ScannerDirection.LONG:
                setup_trigger = max(e1.candle.high, e2.candle.high) + Decimal("0.05") * atr
            else:
                setup_trigger = min(e1.candle.low, e2.candle.low) - Decimal("0.05") * atr
        elif setup is ScannerSetup.BREAKOUT_RETEST:
            if level is None:
                raise ScannerEvaluationError(
                    "STRUCTURE_CONDITION_FAILED", "Breakout level is unavailable"
                )
            setup_trigger = (
                level + Decimal("0.05") * atr
                if direction is ScannerDirection.LONG
                else level - Decimal("0.05") * atr
            )
        elif setup in {
            ScannerSetup.EMA_REJECTION,
            ScannerSetup.LIQUIDITY_SWEEP_REVERSAL,
        }:
            setup_trigger = (
                ctx.s[0].candle.high + Decimal("0.02") * atr
                if direction is ScannerDirection.LONG
                else ctx.s[0].candle.low - Decimal("0.02") * atr
            )
        else:
            if level is None:
                raise ScannerEvaluationError(
                    "STRUCTURE_CONDITION_FAILED", "Compression boundary is unavailable"
                )
            setup_trigger = (
                level + Decimal("0.05") * atr
                if direction is ScannerDirection.LONG
                else level - Decimal("0.05") * atr
            )
        previous_break = _directional_previous_break_level(e1.candle, direction)
        if direction is ScannerDirection.LONG:
            return max(setup_trigger, previous_break)
        return min(setup_trigger, previous_break)

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
        volume0 = _frame_value(s0, "volume_ratio")
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
        if volume0 < Decimal("0.80"):
            raise ScannerEvaluationError(
                "VOLUME_BELOW_MINIMUM", "Trend Pullback volume ratio is below 0.80", "15m"
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
            + Decimal("5") * _n_up(_directional_delta(rsi0, rsi1, direction), D0, Decimal("10"))
            + Decimal("5")
            * _n_up(
                _directional_delta(hist0, hist1, direction) / atr0,
                D0,
                Decimal("0.10"),
            )
        )
        evidence = {
            "pullback_swing_low": min(item.candle.low for item in (s3, s2, s1, s0)),
            "pullback_swing_high": max(item.candle.high for item in (s3, s2, s1, s0)),
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

    def _breakout_retest(self, ctx: EvaluationContext) -> SetupMatch:
        s0 = ctx.s[0]
        direction = ctx.direction
        breakout_found = False
        retest_found = False
        for index in (1, 2, 3):
            breakout = ctx.s[index]
            history = ctx.s[index + 1 : index + 21]
            if len(history) < 20:
                continue
            level = (
                max(item.candle.high for item in history)
                if direction is ScannerDirection.LONG
                else min(item.candle.low for item in history)
            )
            atrb = _frame_value(breakout, "atr14")
            atr0 = _frame_value(s0, "atr14")
            breakout_ok = (
                _directional_break_margin(breakout.candle.close, level, direction)
                >= Decimal("0.10") * atrb
                and _body(breakout.candle) >= Decimal("0.50") * atrb
                and _directional_close_position(breakout.candle, direction) >= Decimal("0.70")
                and _frame_value(breakout, "volume_ratio") >= Decimal("1.50")
            )
            if not breakout_ok:
                continue
            breakout_found = True
            extreme = _directional_extreme(s0.candle, direction)
            retest_ok = (
                abs(extreme - level) <= Decimal("0.20") * atr0
                and _directional_reclaim_margin(s0.candle.close, level, direction)
                >= Decimal("0.05") * atr0
            )
            if not retest_ok:
                continue
            retest_found = True
            if _frame_value(s0, "volume_ratio") < Decimal("0.80"):
                raise ScannerEvaluationError(
                    "VOLUME_BELOW_MINIMUM", "Retest volume ratio is below 0.80", "15m"
                )
            points = (
                Decimal("6.25")
                * _n_up(
                    _directional_break_margin(breakout.candle.close, level, direction) / atrb,
                    Decimal("0.10"),
                    Decimal("0.40"),
                )
                + Decimal("7.50") * _n_down(abs(extreme - level) / atr0, D0, Decimal("0.20"))
                + Decimal("6.25")
                * _n_up(
                    _directional_reclaim_margin(s0.candle.close, level, direction) / atr0,
                    Decimal("0.05"),
                    Decimal("0.40"),
                )
                + Decimal("5")
                * _n_up(
                    _frame_value(breakout, "volume_ratio"),
                    Decimal("1.50"),
                    Decimal("2.50"),
                )
            )
            trigger = self._entry_trigger(ScannerSetup.BREAKOUT_RETEST, ctx, level=level)
            return self._match(
                ScannerSetup.BREAKOUT_RETEST,
                ctx,
                breakout.candle.close_time,
                trigger,
                points,
                level=level,
                evidence={"breakout_close_time": breakout.candle.close_time},
            )
        if breakout_found and not retest_found:
            raise ScannerEvaluationError(
                "RETEST_NOT_CONFIRMED", "No valid retest followed the breakout", "15m"
            )
        raise ScannerEvaluationError(
            "BREAKOUT_NOT_CONFIRMED", "No valid breakout candle was found", "15m"
        )

    def _ema_rejection(self, ctx: EvaluationContext) -> SetupMatch:
        s0, s1 = ctx.s[:2]
        direction = ctx.direction
        atr = _frame_value(s0, "atr14")
        selected = self.selected_ema(s0, direction)
        body = _body(s0.candle)
        if body <= 0:
            raise ScannerEvaluationError("INVALID_15M_OHLCV", "EMA rejection body is zero", "15m")
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
        if _frame_value(s0, "volume_ratio") < D1:
            raise ScannerEvaluationError(
                "VOLUME_BELOW_MINIMUM", "EMA Rejection volume ratio is below 1.00", "15m"
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
            + Decimal("3.75") * _n_up(_frame_value(s0, "volume_ratio"), D1, Decimal("2.50"))
        )
        trigger = self._entry_trigger(ScannerSetup.EMA_REJECTION, ctx)
        return self._match(
            ScannerSetup.EMA_REJECTION,
            ctx,
            s0.candle.close_time,
            trigger,
            points,
            selected_ema=selected,
        )

    def _liquidity_sweep(self, ctx: EvaluationContext) -> SetupMatch:
        s0, s1 = ctx.s[:2]
        direction = ctx.direction
        history = ctx.s[1:11]
        level = (
            min(item.candle.low for item in history)
            if direction is ScannerDirection.LONG
            else max(item.candle.high for item in history)
        )
        atr = _frame_value(s0, "atr14")
        body = _body(s0.candle)
        if body <= 0:
            raise ScannerEvaluationError("INVALID_15M_OHLCV", "Liquidity Sweep body is zero", "15m")
        sweep_depth = _directional_sweep_depth(s0.candle, level, direction)
        rsi0 = _frame_value(s0, "rsi14")
        rsi1 = _frame_value(s1, "rsi14")
        hist0 = _frame_value(s0, "macd_histogram")
        hist1 = _frame_value(s1, "macd_histogram")
        valid = (
            sweep_depth >= Decimal("0.05") * atr
            and _directional_reclaim_margin(s0.candle.close, level, direction)
            >= Decimal("0.05") * atr
            and _directional_wick(s0.candle, direction) / body >= Decimal("1.50")
            and _directional_close_position(s0.candle, direction) >= Decimal("0.70")
            and (
                Decimal("35") <= rsi0 <= Decimal("55")
                if direction is ScannerDirection.LONG
                else Decimal("45") <= rsi0 <= Decimal("65")
            )
            and _directional_delta(rsi0, rsi1, direction) > 0
            and _directional_delta(hist0, hist1, direction) > 0
        )
        if _frame_value(s0, "volume_ratio") < Decimal("1.20"):
            raise ScannerEvaluationError(
                "VOLUME_BELOW_MINIMUM", "Liquidity Sweep volume is below 1.20", "15m"
            )
        if not valid:
            raise ScannerEvaluationError(
                "LIQUIDITY_SWEEP_NOT_CONFIRMED",
                "Liquidity Sweep Reversal formula failed",
                "15m",
            )
        points = Decimal("6.25") * (
            _n_up(sweep_depth / atr, Decimal("0.05"), Decimal("0.40"))
            + _n_up(
                _directional_reclaim_margin(s0.candle.close, level, direction) / atr,
                Decimal("0.05"),
                Decimal("0.40"),
            )
            + _n_up(
                _directional_wick(s0.candle, direction) / body,
                Decimal("1.50"),
                Decimal("4"),
            )
            + _n_up(
                _frame_value(s0, "volume_ratio"),
                Decimal("1.20"),
                Decimal("2.50"),
            )
        )
        trigger = self._entry_trigger(ScannerSetup.LIQUIDITY_SWEEP_REVERSAL, ctx)
        return self._match(
            ScannerSetup.LIQUIDITY_SWEEP_REVERSAL,
            ctx,
            s0.candle.close_time,
            trigger,
            points,
            level=level,
        )

    def _continuation(self, ctx: EvaluationContext) -> SetupMatch:
        s0 = ctx.s[0]
        compression = ctx.s[1:4]
        direction = ctx.direction
        atr = _frame_value(s0, "atr14")
        high = max(item.candle.high for item in compression)
        low = min(item.candle.low for item in compression)
        width = high - low
        each_valid = all(
            _range(item.candle) <= Decimal("0.90") * _frame_value(item, "atr14")
            and (
                item.candle.close > _frame_value(item, "ema20") > _frame_value(item, "ema50")
                if direction is ScannerDirection.LONG
                else item.candle.close < _frame_value(item, "ema20") < _frame_value(item, "ema50")
            )
            for item in compression
        )
        average_volume = sum(
            (_frame_value(item, "volume_ratio") for item in compression), D0
        ) / Decimal("3")
        if not each_valid or width > Decimal("1.50") * atr or average_volume > Decimal("1.10"):
            raise ScannerEvaluationError(
                "CONTINUATION_COMPRESSION_FAILED",
                "Continuation compression formula failed",
                "15m",
            )
        boundary = _directional_compression_boundary(high, low, direction)
        rsi = _frame_value(s0, "rsi14")
        histogram = _frame_value(s0, "macd_histogram")
        breakout = (
            _directional_break_margin(s0.candle.close, boundary, direction) >= Decimal("0.05") * atr
            and _body(s0.candle) >= Decimal("0.40") * atr
            and _directional_close_position(s0.candle, direction) >= Decimal("0.70")
            and (
                Decimal("55") <= rsi <= Decimal("70")
                if direction is ScannerDirection.LONG
                else Decimal("30") <= rsi <= Decimal("45")
            )
            and _directional_histogram(histogram, direction) > 0
            and _directional_delta(
                histogram,
                _frame_value(ctx.s[1], "macd_histogram"),
                direction,
            )
            >= 0
        )
        if _frame_value(s0, "volume_ratio") < Decimal("1.20"):
            raise ScannerEvaluationError(
                "VOLUME_BELOW_MINIMUM", "Continuation volume ratio is below 1.20", "15m"
            )
        if not breakout:
            raise ScannerEvaluationError(
                "CONTINUATION_BREAKOUT_FAILED",
                "Continuation breakout formula failed",
                "15m",
            )
        points = Decimal("6.25") * (
            _n_down(width / atr, Decimal("0.75"), Decimal("1.50"))
            + _n_up(
                _directional_break_margin(s0.candle.close, boundary, direction) / atr,
                Decimal("0.05"),
                Decimal("0.40"),
            )
            + _n_up(_body(s0.candle) / atr, Decimal("0.40"), D1)
            + _n_up(
                _frame_value(s0, "volume_ratio"),
                Decimal("1.20"),
                Decimal("2.50"),
            )
        )
        trigger = self._entry_trigger(ScannerSetup.CONTINUATION_SETUP, ctx, level=boundary)
        return self._match(
            ScannerSetup.CONTINUATION_SETUP,
            ctx,
            s0.candle.close_time,
            trigger,
            points,
            level=boundary,
            evidence={"compression_high": high, "compression_low": low},
        )

    def _match(
        self,
        setup: ScannerSetup,
        ctx: EvaluationContext,
        reference: datetime,
        trigger: Decimal,
        points: Decimal,
        *,
        level: Decimal | None = None,
        selected_ema: Decimal | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> SetupMatch:
        merged_evidence = dict(evidence or {})
        merged_evidence.update(
            {
                "reference_high": ctx.s[0].candle.high,
                "reference_low": ctx.s[0].candle.low,
                "reference_atr15": _frame_value(ctx.s[0], "atr14"),
            }
        )
        return SetupMatch(
            setup=setup,
            reference_close_time=reference,
            setup_confirmed_at=ctx.s[0].candle.close_time,
            expires_at=reference + EXPIRY_LIMITS[setup],
            level=level,
            selected_ema=selected_ema,
            entry_trigger_price=trigger,
            setup_points=points,
            accepted_reasons=(f"{SETUP_NAMES[setup]} confirmed",),
            evidence=merged_evidence,
        )
