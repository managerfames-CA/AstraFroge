"""Scanner Contract Version 1 score, confidence, and invalidation rules."""

from __future__ import annotations

from decimal import Decimal

from app.schemas.scanner import ScannerCandidate, ScannerDirection, ScannerGrade, ScannerSetup
from app.services.scanner_base import (
    D0,
    D1,
    D100,
    EvaluationContext,
    Frame,
    SetupMatch,
    _clamp,
    _directional_break_margin,
    _directional_close_position,
    _directional_delta,
    _directional_ema_extension,
    _directional_histogram,
    _directional_rsi_margin,
    _frame_value,
    _grade,
    _n_down,
    _n_target,
    _n_up,
    _q,
)
from app.services.scanner_contract import SETUP_MINIMUM_VOLUME
from app.services.scanner_setups import ScannerSetupEngine


class ScannerEngine(ScannerSetupEngine):
    """Calculate deterministic score/confidence and setup invalidation."""

    def score(
        self, ctx: EvaluationContext, match: SetupMatch, entry_ready: bool
    ) -> tuple[int, int, ScannerGrade, dict[str, Decimal]]:
        h0, h3 = ctx.h[0], ctx.h[3]
        s0, s1 = ctx.s[0], ctx.s[1]
        e0 = ctx.e[0]
        direction = ctx.direction

        trend = (
            Decimal("7")
            * _n_up(
                abs(_frame_value(h0, "ema20") - _frame_value(h0, "ema50"))
                / _frame_value(h0, "atr14"),
                Decimal("0.25"),
                D1,
            )
            + Decimal("5")
            * _n_up(
                _directional_delta(
                    _frame_value(h0, "ema50"),
                    _frame_value(h3, "ema50"),
                    direction,
                )
                / _frame_value(h0, "atr14"),
                D0,
                Decimal("0.75"),
            )
            + Decimal("4")
            * _n_up(
                _directional_break_margin(
                    h0.candle.close,
                    _frame_value(h0, "ema200"),
                    direction,
                )
                / _frame_value(h0, "atr14"),
                D0,
                Decimal("3"),
            )
            + Decimal("4")
            * _n_up(
                _directional_rsi_margin(_frame_value(h0, "rsi14"), direction),
                Decimal("5"),
                Decimal("15"),
            )
        )

        trigger_margin = _n_up(
            _directional_break_margin(
                e0.candle.close,
                match.entry_trigger_price,
                direction,
            )
            / _frame_value(e0, "atr14"),
            D0,
            Decimal("0.30"),
        )
        extension = abs(
            _directional_ema_extension(
                e0.candle.close,
                _frame_value(e0, "ema20"),
                direction,
            )
        ) / _frame_value(e0, "atr14")
        entry = (
            Decimal("7") * trigger_margin
            + Decimal("4")
            * _n_up(
                _directional_close_position(e0.candle, direction),
                Decimal("0.65"),
                Decimal("0.90"),
            )
            + Decimal("4") * _n_up(_frame_value(e0, "volume_ratio"), Decimal("1.10"), Decimal("2"))
            + Decimal("3")
            * _n_up(
                _directional_histogram(_frame_value(e0, "macd_histogram"), direction)
                / _frame_value(e0, "atr14"),
                D0,
                Decimal("0.10"),
            )
            + Decimal("2") * _n_down(extension, Decimal("0.25"), Decimal("0.75"))
        )

        rsi_target = Decimal("60") if direction is ScannerDirection.LONG else Decimal("40")
        momentum = (
            Decimal("4.5") * _n_target(_frame_value(s0, "rsi14"), rsi_target, Decimal("15"))
            + Decimal("4.5")
            * _n_up(
                _directional_histogram(_frame_value(s0, "macd_histogram"), direction)
                / _frame_value(s0, "atr14"),
                D0,
                Decimal("0.10"),
            )
            + Decimal("3")
            * _n_up(
                _directional_delta(
                    _frame_value(s0, "macd_histogram"),
                    _frame_value(s1, "macd_histogram"),
                    direction,
                )
                / _frame_value(s0, "atr14"),
                D0,
                Decimal("0.10"),
            )
            + Decimal("3")
            * _n_up(
                _directional_histogram(_frame_value(e0, "macd_histogram"), direction)
                / _frame_value(e0, "atr14"),
                D0,
                Decimal("0.10"),
            )
        )

        volume = Decimal("4") * _n_up(
            _frame_value(s0, "volume_ratio"),
            SETUP_MINIMUM_VOLUME[match.setup],
            Decimal("2.50"),
        ) + Decimal("6") * _n_up(
            _frame_value(e0, "volume_ratio"),
            Decimal("1.10"),
            Decimal("2.50"),
        )

        quote_volume_ratio = ctx.universe.quote_volume / Decimal("10000000")
        quote_quality = _clamp(quote_volume_ratio.log10() / Decimal("2"))
        spread_quality = _clamp((Decimal("10") - ctx.universe.spread_bps) / Decimal("10"))
        liquidity = Decimal("3") * quote_quality + Decimal("2") * spread_quality
        freshness = (
            Decimal("5")
            * (ctx.freshness["1h"] + ctx.freshness["15m"] + ctx.freshness["5m"])
            / Decimal("3")
        )

        components = {
            "trend": trend,
            "setup": match.setup_points,
            "entry": entry,
            "momentum": momentum,
            "volume": volume,
            "liquidity": liquidity,
            "freshness": freshness,
        }
        raw_score = _clamp(sum(components.values(), D0), D0, D100)

        data_completeness = sum(
            (min(Decimal(ctx.counts[key]) / Decimal("250"), D1) for key in ("1h", "15m", "5m")),
            D0,
        ) / Decimal("3")
        freshness_margin = sum(ctx.freshness.values(), D0) / Decimal("3")
        rule_margin = Decimal("0.60") * (match.setup_points / Decimal("25")) + Decimal("0.40") * (
            entry / Decimal("20")
        )

        # The first three votes are one because EvaluationContext is created only
        # after the exact regime EMA-stack, structure, and MACD gates pass.
        vote_1h_ema_stack = D1
        vote_1h_structure = D1
        vote_1h_macd = D1
        vote_15m_close_ema20 = Decimal(
            int(
                s0.candle.close > _frame_value(s0, "ema20")
                if direction is ScannerDirection.LONG
                else s0.candle.close < _frame_value(s0, "ema20")
            )
        )
        vote_15m_histogram = Decimal(
            int(_directional_histogram(_frame_value(s0, "macd_histogram"), direction) > 0)
        )
        vote_5m_histogram = Decimal(
            int(_directional_histogram(_frame_value(e0, "macd_histogram"), direction) > 0)
        )
        votes = (
            vote_1h_ema_stack
            + vote_1h_structure
            + vote_1h_macd
            + vote_15m_close_ema20
            + vote_15m_histogram
            + vote_5m_histogram
        )
        confidence_raw = (
            Decimal("25") * data_completeness
            + Decimal("20") * freshness_margin
            + Decimal("25") * rule_margin
            + Decimal("20") * (votes / Decimal("6"))
            + Decimal("10") * (liquidity / Decimal("5"))
        )
        confidence = _q(_clamp(confidence_raw, D0, D100))
        effective = _q(raw_score)
        if not entry_ready or 60 <= confidence <= 69:
            effective = min(effective, 84)
        return effective, confidence, _grade(effective), components

    def invalidated(self, candidate: ScannerCandidate, s0: Frame, e0: Frame) -> bool:
        """Apply the exact stored-setup invalidation formula."""

        direction = candidate.direction
        atr15 = _frame_value(s0, "atr14")
        atr5 = _frame_value(e0, "atr14")
        if candidate.setup is ScannerSetup.TREND_PULLBACK:
            ema50 = _frame_value(s0, "ema50")
            swing_low = Decimal(str(candidate.evidence["pullback_swing_low"]))
            swing_high = Decimal(str(candidate.evidence["pullback_swing_high"]))
            if direction is ScannerDirection.LONG:
                return (
                    s0.candle.close < ema50 - Decimal("0.25") * atr15
                    or e0.candle.close < swing_low - Decimal("0.10") * atr5
                )
            return (
                s0.candle.close > ema50 + Decimal("0.25") * atr15
                or e0.candle.close > swing_high + Decimal("0.10") * atr5
            )
        if candidate.setup is ScannerSetup.BREAKOUT_RETEST and candidate.level is not None:
            if direction is ScannerDirection.LONG:
                return s0.candle.close < candidate.level - Decimal("0.15") * atr15
            return s0.candle.close > candidate.level + Decimal("0.15") * atr15
        if candidate.setup is ScannerSetup.EMA_REJECTION and candidate.selected_ema is not None:
            reference_low = Decimal(
                str(
                    candidate.evidence.get(
                        "reference_low",
                        candidate.evidence.get("reference_extreme", s0.candle.low),
                    )
                )
            )
            reference_high = Decimal(
                str(
                    candidate.evidence.get(
                        "reference_high",
                        candidate.evidence.get("reference_extreme", s0.candle.high),
                    )
                )
            )
            if direction is ScannerDirection.LONG:
                return (
                    s0.candle.close < candidate.selected_ema - Decimal("0.20") * atr15
                    or s0.candle.close < reference_low - Decimal("0.05") * atr15
                )
            return (
                s0.candle.close > candidate.selected_ema + Decimal("0.20") * atr15
                or s0.candle.close > reference_high + Decimal("0.05") * atr15
            )
        if candidate.setup is ScannerSetup.LIQUIDITY_SWEEP_REVERSAL:
            reference_low = Decimal(
                str(
                    candidate.evidence.get(
                        "reference_low",
                        candidate.evidence.get("reference_extreme", s0.candle.low),
                    )
                )
            )
            reference_high = Decimal(
                str(
                    candidate.evidence.get(
                        "reference_high",
                        candidate.evidence.get("reference_extreme", s0.candle.high),
                    )
                )
            )
            if direction is ScannerDirection.LONG:
                return e0.candle.close < reference_low - Decimal("0.05") * atr5
            return e0.candle.close > reference_high + Decimal("0.05") * atr5
        if candidate.setup is ScannerSetup.CONTINUATION_SETUP and candidate.level is not None:
            if direction is ScannerDirection.LONG:
                return s0.candle.close < candidate.level - Decimal("0.15") * atr15
            return s0.candle.close > candidate.level + Decimal("0.15") * atr15
        return False
