"""Scanner runtime, lifecycle, failure, and cadence tests."""

from __future__ import annotations

from tests.unit.scanner_test_support import (
    NOW,
    Any,
    CandidateLifecycle,
    Decimal,
    EvaluationContext,
    FailingUniverse,
    FakeClock,
    FakeIndicators,
    FakeMarket,
    FakeUniverse,
    MarketCandleSeries,
    MethodType,
    NoTimeMarket,
    ScannerAuditRecord,
    ScannerDirection,
    ScannerEngine,
    ScannerEvaluationError,
    ScannerGrade,
    ScannerRunStatus,
    ScannerService,
    ScannerSetup,
    ScannerState,
    SkewMarket,
    UniverseCandidate,
    UniverseSnapshot,
    _candidate_for_service,
    _candidate_key,
    _prepare_setup,
    _series,
    asyncio,
    base_context,
    datetime,
    frame,
    pytest,
    timedelta,
    universe,
)


def test_scanner_state_run_now_duplicate_and_stop() -> None:
    async def scenario() -> None:
        service = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),  # type: ignore[arg-type]
        )

        async def evaluate(
            self: ScannerService,
            item: UniverseCandidate,
            exchange_time: datetime,
            run_id: str,
        ) -> tuple[Any, list[ScannerAuditRecord], EvaluationContext]:
            ctx = _prepare_setup(ScannerDirection.LONG, ScannerSetup.TREND_PULLBACK)
            match = self._engine._trend_pullback(ctx)
            score, confidence, grade, components = self._engine.score(ctx, match, False)
            from app.schemas.scanner import ScannerCandidate

            candidate = ScannerCandidate(
                candidate_id=_candidate_key(
                    item.symbol,
                    ctx.direction,
                    match.setup,
                    match.reference_close_time,
                ),
                symbol=item.symbol,
                direction=ctx.direction,
                setup=match.setup,
                setup_name="Trend Pullback",
                reference_close_time=match.reference_close_time,
                setup_confirmed_at=match.setup_confirmed_at,
                expires_at=match.expires_at,
                lifecycle=CandidateLifecycle.WATCH_NEAR,
                score=score,
                confidence=confidence,
                grade=grade,
                entry_ready=False,
                universe_rank=item.rank,
                quote_volume=item.quote_volume,
                spread_bps=item.spread_bps,
                entry_trigger_price=match.entry_trigger_price,
                evaluated_at=NOW,
                evidence={"reference_extreme": "99"},
                score_components=components,
            )
            return candidate, [], ctx

        service._evaluate_symbol = MethodType(evaluate, service)  # type: ignore[method-assign]
        assert service.status().state is ScannerState.OFF
        first = await service.run_now()
        assert first.status is ScannerRunStatus.COMPLETED
        second = await service.run_now()
        assert second.updated_candidates == 1
        status = await service.start()
        assert status.state is ScannerState.ON
        stopped = await service.stop()
        assert stopped.state is ScannerState.OFF

    asyncio.run(scenario())


def test_shared_lock_skips_overlap() -> None:
    async def scenario() -> None:
        service = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),  # type: ignore[arg-type]
        )
        await service._lock.acquire()
        try:
            run = await service.run_now()
        finally:
            service._lock.release()
        assert run.status is ScannerRunStatus.SKIPPED
        assert run.audits[0].code == "SCAN_ALREADY_RUNNING"

    asyncio.run(scenario())


def test_align_integrity_freshness_and_failures() -> None:
    engine = ScannerEngine()
    candles, indicators = _series("15m")
    frames, freshness = engine.align(candles, indicators, exchange_time=NOW)
    assert len(frames) == 200
    assert frames[0].candle.close_time == NOW
    assert freshness == Decimal("1")

    stale_candles, stale_indicators = _series("15m", stale=True)
    with pytest.raises(ScannerEvaluationError) as stale:
        engine.align(stale_candles, stale_indicators, exchange_time=NOW)
    assert stale.value.code == "STALE_15M_DATA"

    short_candles, short_indicators = _series("5m", count=199)
    with pytest.raises(ScannerEvaluationError) as short:
        engine.align(short_candles, short_indicators, exchange_time=NOW)
    assert short.value.code == "INSUFFICIENT_5M_HISTORY"

    broken_candles, broken_indicators = _series("1h")
    broken_candles.candles[100] = broken_candles.candles[100].model_copy(
        update={"close_time": broken_candles.candles[99].close_time}
    )
    with pytest.raises(ScannerEvaluationError) as broken:
        engine.align(broken_candles, broken_indicators, exchange_time=NOW)
    assert broken.value.code in {"MISSING_1H_CANDLES", "INVALID_1H_OHLCV"}

    missing_candles, missing_indicators = _series("15m")
    missing_indicators.points[-1] = missing_indicators.points[-1].model_copy(update={"atr14": None})
    with pytest.raises(ScannerEvaluationError) as missing:
        engine.align(missing_candles, missing_indicators, exchange_time=NOW)
    assert missing.value.code == "MISSING_REQUIRED_INDICATOR"


def test_volatility_and_mixed_regime_fail_closed() -> None:
    engine = ScannerEngine()
    low = frame("1000", atr="1")
    with pytest.raises(ScannerEvaluationError) as below:
        engine.volatility(low, "15m")
    assert below.value.code == "VOLATILITY_BELOW_MINIMUM"
    high = frame("10", atr="1")
    with pytest.raises(ScannerEvaluationError) as above:
        engine.volatility(high, "5m")
    assert above.value.code == "VOLATILITY_ABOVE_MAXIMUM"

    mixed = base_context(ScannerDirection.LONG).h
    mixed[0] = frame(
        "100",
        ema20="99",
        ema50="98",
        ema200="97",
        rsi="60",
        macd="1",
        signal="0",
        histogram="0.2",
        interval_minutes=60,
    )
    mixed[3] = frame(
        "100",
        ema20="100",
        ema50="98",
        ema200="97",
        rsi="60",
        macd="1",
        signal="0",
        histogram="0.2",
        interval_minutes=60,
        minutes_ago=180,
    )
    with pytest.raises(ScannerEvaluationError) as exc:
        engine.regime(mixed, "bullish")
    assert exc.value.code == "TREND_MIXED"


def test_full_scan_market_and_universe_failures() -> None:
    async def scenario() -> None:
        no_time = ScannerService(
            NoTimeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),  # type: ignore[arg-type]
        )
        run = await no_time.run_now()
        assert run.status is ScannerRunStatus.FAILED
        assert run.audits[0].code == "MARKET_TIME_UNAVAILABLE"

        skew = ScannerService(
            SkewMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),  # type: ignore[arg-type]
        )
        run = await skew.run_now()
        assert run.audits[0].code == "CLOCK_SKEW_EXCEEDED"

        unavailable = ScannerService(
            FakeMarket(),
            FailingUniverse(),
            FakeIndicators(),
            clock=FakeClock(),  # type: ignore[arg-type]
        )
        run = await unavailable.run_now()
        assert run.audits[0].code == "UNIVERSE_UNAVAILABLE"

    asyncio.run(scenario())


def test_full_scan_partial_and_total_symbol_failure() -> None:
    async def scenario() -> None:
        service = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),  # type: ignore[arg-type]
        )

        async def fail(
            self: ScannerService,
            item: UniverseCandidate,
            exchange_time: datetime,
            run_id: str,
        ) -> tuple[Any, list[ScannerAuditRecord], EvaluationContext]:
            raise ScannerEvaluationError("MISSING_5M_CANDLES", "none", "5m")

        service._evaluate_symbol = MethodType(fail, service)  # type: ignore[method-assign]
        run = await service.run_now()
        assert run.status is ScannerRunStatus.FAILED
        assert any(item.code == "FULL_MARKET_DATA_FAILURE" for item in run.audits)

        class TwoUniverse(FakeUniverse):
            async def build(self) -> UniverseSnapshot:
                snapshot = await super().build()
                second = snapshot.candidates[0].model_copy(
                    update={"rank": 2, "symbol": "ETHUSDT", "base_asset": "ETH"}
                )
                return snapshot.model_copy(
                    update={"eligible_count": 2, "candidates": [snapshot.candidates[0], second]}
                )

        service = ScannerService(
            FakeMarket(),
            TwoUniverse(),
            FakeIndicators(),
            clock=FakeClock(),  # type: ignore[arg-type]
        )
        good = _candidate_for_service()

        async def partial(
            self: ScannerService,
            item: UniverseCandidate,
            exchange_time: datetime,
            run_id: str,
        ) -> tuple[Any, list[ScannerAuditRecord], EvaluationContext]:
            if item.symbol == "ETHUSDT":
                raise ScannerEvaluationError("MISSING_5M_CANDLES", "none", "5m")
            return (
                good.model_copy(update={"symbol": item.symbol}),
                [],
                _prepare_setup(
                    ScannerDirection.LONG,
                    ScannerSetup.TREND_PULLBACK,
                ),
            )

        service._evaluate_symbol = MethodType(partial, service)  # type: ignore[method-assign]
        run = await service.run_now()
        assert run.status is ScannerRunStatus.DEGRADED
        assert run.failed_symbols == 1

    asyncio.run(scenario())


def test_full_scan_valid_no_setup_is_completed_rejection() -> None:
    async def scenario() -> None:
        service = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),  # type: ignore[arg-type]
        )

        async def reject(
            self: ScannerService,
            item: UniverseCandidate,
            exchange_time: datetime,
            run_id: str,
        ) -> tuple[Any, list[ScannerAuditRecord], EvaluationContext]:
            return (
                None,
                [
                    ScannerAuditRecord(
                        code="SETUP_NOT_DETECTED",
                        detail="No approved deterministic setup matched",
                        symbol=item.symbol,
                        direction=ScannerDirection.LONG,
                        timeframe="15m",
                    )
                ],
                _prepare_setup(ScannerDirection.LONG, ScannerSetup.TREND_PULLBACK),
            )

        service._evaluate_symbol = MethodType(reject, service)  # type: ignore[method-assign]
        run = await service.run_now()
        assert run.status is ScannerRunStatus.COMPLETED
        assert run.successful_symbols == 1
        assert run.failed_symbols == 0
        assert any(item.code == "SETUP_NOT_DETECTED" for item in run.audits)
        assert not any(item.code == "FULL_MARKET_DATA_FAILURE" for item in run.audits)

    asyncio.run(scenario())


def test_active_refresh_expiry_invalidation_and_no_downgrade() -> None:
    async def scenario() -> None:
        service = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),  # type: ignore[arg-type]
        )
        expired = _candidate_for_service(expires_at=NOW)
        service._candidates[expired.candidate_id] = expired
        run = await service.active_refresh()
        assert run.status is ScannerRunStatus.COMPLETED
        assert expired.lifecycle is CandidateLifecycle.EXPIRED
        assert expired.candidate_id in service._terminal_keys

        qualified = _candidate_for_service(lifecycle=CandidateLifecycle.QUALIFIED)
        service._candidates = {qualified.candidate_id: qualified}
        service._engine.invalidated = lambda candidate, s0, e0: False  # type: ignore[method-assign]
        run = await service.active_refresh()
        assert run.status is ScannerRunStatus.COMPLETED
        assert qualified.lifecycle is CandidateLifecycle.QUALIFIED

        invalid = _candidate_for_service()
        service._candidates = {invalid.candidate_id: invalid}
        service._engine.invalidated = lambda candidate, s0, e0: True  # type: ignore[method-assign]
        await service.active_refresh()
        assert invalid.lifecycle is CandidateLifecycle.INVALIDATED

    asyncio.run(scenario())


def test_setup_dispatch_and_invalidation_rules() -> None:
    engine = ScannerEngine()
    ctx = _prepare_setup(ScannerDirection.LONG, ScannerSetup.TREND_PULLBACK)
    assert any(item.setup is ScannerSetup.TREND_PULLBACK for item in engine.setups(ctx))

    base = _candidate_for_service()
    s0 = frame("90", ema50="100")
    e0 = frame("90", interval_minutes=5)
    assert engine.invalidated(base, s0, e0)

    breakout = base.model_copy(
        update={"setup": ScannerSetup.BREAKOUT_RETEST, "level": Decimal("100")}
    )
    assert engine.invalidated(breakout, frame("99", atr="1"), e0)

    ema = base.model_copy(
        update={
            "setup": ScannerSetup.EMA_REJECTION,
            "selected_ema": Decimal("100"),
            "evidence": {"reference_extreme": "99"},
        }
    )
    assert engine.invalidated(ema, frame("99", atr="1"), frame("98", atr="1", interval_minutes=5))

    sweep = base.model_copy(
        update={
            "setup": ScannerSetup.LIQUIDITY_SWEEP_REVERSAL,
            "evidence": {"reference_extreme": "99"},
        }
    )
    assert engine.invalidated(sweep, frame("100"), frame("98", atr="1", interval_minutes=5))

    continuation = base.model_copy(
        update={"setup": ScannerSetup.CONTINUATION_SETUP, "level": Decimal("100")}
    )
    assert engine.invalidated(continuation, frame("99", atr="1"), e0)

    harmless = base.model_copy(update={"setup": ScannerSetup.BREAKOUT_RETEST, "level": None})
    assert not engine.invalidated(harmless, frame("100"), frame("100", interval_minutes=5))


@pytest.mark.parametrize(
    ("score_result", "entry_ready", "expected", "expected_code"),
    [
        ((90, 59, ScannerGrade.A_PLUS), True, CandidateLifecycle.REJECTED, "CONFIDENCE_BELOW_60"),
        ((79, 75, ScannerGrade.REJECT), True, CandidateLifecycle.REJECTED, "SCORE_BELOW_80"),
        ((90, 75, ScannerGrade.A_PLUS), True, CandidateLifecycle.QUALIFIED, None),
        ((84, 75, ScannerGrade.B_PLUS), False, CandidateLifecycle.WATCH_NEAR, "ENTRY_NOT_READY"),
        (
            (90, 65, ScannerGrade.A_PLUS),
            False,
            CandidateLifecycle.WATCH_NEAR,
            "CONFIDENCE_WATCH_ONLY",
        ),
    ],
)
def test_evaluate_symbol_lifecycle_boundaries(
    score_result: tuple[int, int, ScannerGrade],
    entry_ready: bool,
    expected: CandidateLifecycle,
    expected_code: str | None,
) -> None:
    async def scenario() -> None:
        service = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),  # type: ignore[arg-type]
        )
        ctx = _prepare_setup(ScannerDirection.LONG, ScannerSetup.TREND_PULLBACK)
        match = service._engine._trend_pullback(ctx)
        from dataclasses import replace

        match = replace(match, setup_confirmed_at=NOW - timedelta(minutes=15))

        async def load(
            self: ScannerService, item: UniverseCandidate, exchange_time: datetime
        ) -> EvaluationContext:
            return ctx

        service._load_context = MethodType(load, service)  # type: ignore[method-assign]
        service._engine.evaluate_setups = (  # type: ignore[method-assign]
            lambda loaded: ([match], [])
        )
        service._engine.shared_entry = (  # type: ignore[method-assign]
            lambda e, direction, trigger: entry_ready
        )
        service._engine.score = (  # type: ignore[method-assign]
            lambda loaded, accepted, ready: (
                score_result[0],
                score_result[1],
                score_result[2],
                {"trend": Decimal("1")},
            )
        )
        candidate, audits, _ = await service._evaluate_symbol(universe(), NOW, "run-1")
        assert candidate is not None
        assert candidate.lifecycle is expected
        assert audits == []
        if expected_code is not None:
            assert expected_code in candidate.audit_codes
        if expected is CandidateLifecycle.QUALIFIED:
            assert candidate.qualification_expires_at is not None

    asyncio.run(scenario())


def test_evaluate_symbol_superseded_and_no_setup() -> None:
    async def scenario() -> None:
        service = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),  # type: ignore[arg-type]
        )
        ctx = _prepare_setup(ScannerDirection.LONG, ScannerSetup.TREND_PULLBACK)
        first = service._engine._trend_pullback(ctx)
        second = first.__class__(
            setup=ScannerSetup.CONTINUATION_SETUP,
            reference_close_time=first.reference_close_time,
            setup_confirmed_at=first.setup_confirmed_at,
            expires_at=first.expires_at,
            level=Decimal("100"),
            selected_ema=None,
            entry_trigger_price=first.entry_trigger_price,
            setup_points=Decimal("20"),
            accepted_reasons=("Continuation confirmed",),
            evidence={},
        )

        async def load(
            self: ScannerService, item: UniverseCandidate, exchange_time: datetime
        ) -> EvaluationContext:
            return ctx

        service._load_context = MethodType(load, service)  # type: ignore[method-assign]
        service._engine.evaluate_setups = (  # type: ignore[method-assign]
            lambda loaded: ([first, second], [])
        )
        service._engine.shared_entry = lambda e, d, t: False  # type: ignore[method-assign]
        service._engine.score = (  # type: ignore[method-assign]
            lambda loaded, accepted, ready: (
                84 if accepted.setup is ScannerSetup.TREND_PULLBACK else 80,
                75,
                ScannerGrade.B_PLUS,
                {},
            )
        )
        candidate, audits, _ = await service._evaluate_symbol(universe(), NOW, "run-1")
        assert candidate is not None
        assert candidate.setup is ScannerSetup.TREND_PULLBACK
        assert audits[0].code == "SUPERSEDED_BY_HIGHER_RANKED_SETUP"

        service._engine.evaluate_setups = lambda loaded: ([], [])  # type: ignore[method-assign]
        candidate, audits, _ = await service._evaluate_symbol(universe(), NOW, "run-1")
        assert candidate is None
        assert audits[0].code == "SETUP_NOT_DETECTED"

    asyncio.run(scenario())


def test_active_refresh_ready_and_failure_paths() -> None:
    async def scenario() -> None:
        service = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),  # type: ignore[arg-type]
        )
        watch = _candidate_for_service()
        watch.audit_codes = ["ENTRY_NOT_READY", "GRADE_B_PLUS_WATCH_ONLY", "CONFIDENCE_WATCH_ONLY"]
        service._candidates[watch.candidate_id] = watch
        service._candidate_contexts[watch.candidate_id] = _prepare_setup(
            ScannerDirection.LONG,
            ScannerSetup.TREND_PULLBACK,
        )
        service._engine.invalidated = lambda candidate, s0, e0: False  # type: ignore[method-assign]
        service._engine.shared_entry = lambda e, d, t: True  # type: ignore[method-assign]
        service._engine.score = (  # type: ignore[method-assign]
            lambda ctx, match, entry_ready: (
                90,
                75,
                ScannerGrade.A_PLUS,
                {"setup": Decimal("20")},
            )
        )
        run = await service.active_refresh()
        assert watch.lifecycle is CandidateLifecycle.QUALIFIED
        assert watch.audit_codes == []
        assert run.qualified_candidates == 1

        b_plus = _candidate_for_service()
        b_plus.score = 84
        b_plus.grade = ScannerGrade.B_PLUS
        b_plus.audit_codes = ["GRADE_B_PLUS_WATCH_ONLY"]
        service._candidates = {b_plus.candidate_id: b_plus}
        service._candidate_contexts = {
            b_plus.candidate_id: _prepare_setup(
                ScannerDirection.LONG,
                ScannerSetup.TREND_PULLBACK,
            )
        }
        service._engine.score = (  # type: ignore[method-assign]
            lambda ctx, match, entry_ready: (
                84,
                75,
                ScannerGrade.B_PLUS,
                {"setup": Decimal("20")},
            )
        )
        await service.active_refresh()
        assert b_plus.lifecycle is CandidateLifecycle.WATCH_NEAR
        assert b_plus.audit_codes == ["GRADE_B_PLUS_WATCH_ONLY"]

        failed = _candidate_for_service()
        service = ScannerService(
            NoTimeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),  # type: ignore[arg-type]
        )
        service._candidates[failed.candidate_id] = failed
        run = await service.active_refresh()
        assert run.status is ScannerRunStatus.FAILED
        assert run.audits[0].code == "MARKET_TIME_UNAVAILABLE"

        service = ScannerService(
            FakeMarket(),
            FakeUniverse(),
            FakeIndicators(),
            clock=FakeClock(),  # type: ignore[arg-type]
        )
        service._candidates[failed.candidate_id] = failed
        service._candidate_contexts[failed.candidate_id] = _prepare_setup(
            ScannerDirection.LONG,
            ScannerSetup.TREND_PULLBACK,
        )

        async def bad_candles(symbol: str, interval: str, limit: int) -> MarketCandleSeries:
            raise ScannerEvaluationError("STALE_5M_DATA", "stale", "5m")

        service._market.candles = bad_candles  # type: ignore[method-assign]
        run = await service.active_refresh()
        assert run.status is ScannerRunStatus.FAILED
        assert any(item.code == "STALE_5M_DATA" for item in run.audits)

    asyncio.run(scenario())
