"""Scanner formulas, boundaries, and setup evaluators."""

from __future__ import annotations

from tests.unit.scanner_test_support import (
    EXPIRY_LIMITS,
    NOW,
    Decimal,
    ScannerDirection,
    ScannerEngine,
    ScannerEvaluationError,
    ScannerGrade,
    ScannerSetup,
    _candidate_key,
    _directional_break_margin,
    _directional_close_position,
    _directional_delta,
    _directional_extreme,
    _directional_histogram,
    _directional_rsi_margin,
    _directional_wick,
    _grade,
    _prepare_setup,
    _q,
    base_context,
    candle,
    frame,
    pytest,
)


@pytest.mark.parametrize(
    ("direction", "wick", "extreme", "break_margin", "delta", "histogram", "rsi_margin"),
    [
        (
            ScannerDirection.LONG,
            Decimal("0.1"),
            Decimal("99.4"),
            Decimal("2"),
            Decimal("2"),
            Decimal("2"),
            Decimal("10"),
        ),
        (
            ScannerDirection.SHORT,
            Decimal("0.2"),
            Decimal("100.2"),
            Decimal("-2"),
            Decimal("-2"),
            Decimal("-2"),
            Decimal("-10"),
        ),
    ],
)
def test_directional_helpers(
    direction: ScannerDirection,
    wick: Decimal,
    extreme: Decimal,
    break_margin: Decimal,
    delta: Decimal,
    histogram: Decimal,
    rsi_margin: Decimal,
) -> None:
    item = candle()
    assert _directional_wick(item, direction) == wick
    assert _directional_extreme(item, direction) == extreme
    assert _directional_break_margin(Decimal("102"), Decimal("100"), direction) == break_margin
    assert _directional_delta(Decimal("102"), Decimal("100"), direction) == delta
    assert _directional_histogram(Decimal("2"), direction) == histogram
    assert _directional_rsi_margin(Decimal("60"), direction) == rsi_margin
    assert Decimal("0") <= _directional_close_position(item, direction) <= Decimal("1")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("79"), ScannerGrade.REJECT),
        (Decimal("80"), ScannerGrade.B_PLUS),
        (Decimal("84"), ScannerGrade.B_PLUS),
        (Decimal("85"), ScannerGrade.A),
        (Decimal("89"), ScannerGrade.A),
        (Decimal("90"), ScannerGrade.A_PLUS),
        (Decimal("100"), ScannerGrade.A_PLUS),
    ],
)
def test_grade_boundaries(value: Decimal, expected: ScannerGrade) -> None:
    assert _grade(int(value)) is expected


def test_round_half_up_and_candidate_identity() -> None:
    assert _q(Decimal("84.5")) == 85
    first = _candidate_key("btcusdt", ScannerDirection.LONG, ScannerSetup.TREND_PULLBACK, NOW)
    second = _candidate_key("BTCUSDT", ScannerDirection.LONG, ScannerSetup.TREND_PULLBACK, NOW)
    assert first == second
    assert len(first) == 64


def test_selected_ema_tie_prefers_ema20_and_missing_fails() -> None:
    engine = ScannerEngine()
    tied = frame(low="99.8", high="100.2", ema20="100", ema50="99.6")
    assert engine.selected_ema(tied, ScannerDirection.LONG) == Decimal("100")
    missing = frame(low="90", high="91", ema20="100", ema50="99")
    with pytest.raises(ScannerEvaluationError, match="No EMA"):
        engine.selected_ema(missing, ScannerDirection.LONG)


def test_regime_is_mutually_exclusive() -> None:
    engine = ScannerEngine()
    bullish = base_context(ScannerDirection.LONG).h
    bullish[3] = frame(
        "104",
        ema20="104",
        ema50="99",
        ema200="89",
        rsi="60",
        macd="1",
        signal="0",
        histogram="0.2",
        interval_minutes=60,
        minutes_ago=180,
    )
    assert engine.regime(bullish, "bullish") is ScannerDirection.LONG
    with pytest.raises(ScannerEvaluationError) as exc:
        engine.regime(bullish, "range")
    assert exc.value.code == "TREND_SIDEWAYS"


@pytest.mark.parametrize("direction", [ScannerDirection.LONG, ScannerDirection.SHORT])
def test_shared_entry_long_and_short(direction: ScannerDirection) -> None:
    engine = ScannerEngine()
    ctx = base_context(direction)
    if direction is ScannerDirection.LONG:
        ctx.e[0] = frame(
            "102",
            open_="100",
            high="102.1",
            low="100",
            ema20="101.4",
            ema50="100",
            rsi="60",
            macd="1",
            signal="0",
            histogram="0.2",
            volume_ratio="1.5",
            interval_minutes=5,
        )
        ctx.e[1] = frame(
            "100",
            high="101",
            low="99",
            ema20="99",
            ema50="98",
            rsi="60",
            histogram="0.1",
            interval_minutes=5,
            minutes_ago=5,
        )
        trigger = Decimal("101.5")
    else:
        ctx.e[0] = frame(
            "98",
            open_="100",
            high="100",
            low="97.9",
            ema20="98.6",
            ema50="100",
            rsi="40",
            macd="-1",
            signal="0",
            histogram="-0.2",
            volume_ratio="1.5",
            interval_minutes=5,
        )
        ctx.e[1] = frame(
            "100",
            high="101",
            low="99",
            ema20="101",
            ema50="102",
            rsi="40",
            histogram="-0.1",
            interval_minutes=5,
            minutes_ago=5,
        )
        trigger = Decimal("98.5")
    assert engine.shared_entry(ctx.e, direction, trigger)


@pytest.mark.parametrize("setup", list(ScannerSetup))
@pytest.mark.parametrize("direction", list(ScannerDirection))
def test_all_five_setup_formulas_long_and_short(
    setup: ScannerSetup, direction: ScannerDirection
) -> None:
    engine = ScannerEngine()
    ctx = _prepare_setup(direction, setup)
    evaluator = {
        ScannerSetup.TREND_PULLBACK: engine._trend_pullback,
        ScannerSetup.BREAKOUT_RETEST: engine._breakout_retest,
        ScannerSetup.EMA_REJECTION: engine._ema_rejection,
        ScannerSetup.LIQUIDITY_SWEEP_REVERSAL: engine._liquidity_sweep,
        ScannerSetup.CONTINUATION_SETUP: engine._continuation,
    }[setup]
    match = evaluator(ctx)
    assert match.setup is setup
    assert match.expires_at == match.reference_close_time + EXPIRY_LIMITS[setup]
    score, confidence, grade, components = engine.score(ctx, match, entry_ready=False)
    assert 0 <= score <= 84
    assert 0 <= confidence <= 100
    assert grade in set(ScannerGrade)
    assert set(components) == {
        "trend",
        "setup",
        "entry",
        "momentum",
        "volume",
        "liquidity",
        "freshness",
    }
