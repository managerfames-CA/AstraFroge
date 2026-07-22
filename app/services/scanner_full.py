"""Full Universe Scan and per-symbol deterministic candidate discovery."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from app.schemas.scanner import (
    CandidateLifecycle,
    ScannerAuditRecord,
    ScannerCandidate,
    ScannerGrade,
    ScannerRunStatus,
    ScannerRunSummary,
    ScannerRunType,
    ScannerState,
)
from app.schemas.universe import UniverseCandidate
from app.services.scanner_base import (
    EvaluationContext,
    Frame,
    ScannerEvaluationError,
    SetupMatch,
    _candidate_key,
    _directional_ema_extension,
    _frame_value,
)
from app.services.scanner_contract import (
    FULL_SCAN_INTERVAL,
    MAX_CLOCK_SKEW,
    MAX_SELECTED_CANDIDATES,
    QUALIFICATION_EXPIRY,
    REENTRY_COOLDOWN,
    SETUP_NAMES,
    UNIVERSE_MAX_AGE,
)
from app.services.scanner_runtime import ScannerRuntimeBase, next_five_minute_boundary
from app.services.scanner_scoring import ScannerEngine

_SYMBOL_DATA_FAILURE_CODES = {
    "MISSING_1H_CANDLES",
    "MISSING_15M_CANDLES",
    "MISSING_5M_CANDLES",
    "INSUFFICIENT_1H_HISTORY",
    "INSUFFICIENT_15M_HISTORY",
    "INSUFFICIENT_5M_HISTORY",
    "STALE_1H_DATA",
    "STALE_15M_DATA",
    "STALE_5M_DATA",
    "INVALID_1H_OHLCV",
    "INVALID_15M_OHLCV",
    "INVALID_5M_OHLCV",
    "MISSING_REQUIRED_INDICATOR",
    "INDICATOR_CALCULATION_FAILED",
    "STRUCTURE_INSUFFICIENT",
    "UNIVERSE_ELIGIBILITY_FAILED",
}


def _is_symbol_data_failure(code: str) -> bool:
    return code in _SYMBOL_DATA_FAILURE_CODES


def _candidate_order(candidate: ScannerCandidate) -> tuple[object, ...]:
    lifecycle_rank = {
        CandidateLifecycle.QUALIFIED: 0,
        CandidateLifecycle.WATCH_NEAR: 1,
        CandidateLifecycle.DETECTED: 2,
        CandidateLifecycle.REJECTED: 3,
        CandidateLifecycle.INVALIDATED: 4,
        CandidateLifecycle.EXPIRED: 5,
    }[candidate.lifecycle]
    return (
        lifecycle_rank,
        -(candidate.score if candidate.score is not None else -1),
        -(candidate.confidence if candidate.confidence is not None else -1),
        candidate.universe_rank,
        candidate.spread_bps,
        -candidate.quote_volume,
        candidate.symbol,
        candidate.setup.value,
        candidate.reference_close_time,
    )


class ScannerFullService(ScannerRuntimeBase):
    """Implement Full Universe Scan, deterministic selection, and deduplication."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._engine = ScannerEngine()

    async def full_scan(self) -> ScannerRunSummary:
        if self._run_active or self._lock.locked():
            return await self._skipped(ScannerRunType.FULL_UNIVERSE_SCAN)
        self._run_active = True
        local_started = self._clock.now()
        run = ScannerRunSummary(
            run_id=str(uuid4()),
            run_type=ScannerRunType.FULL_UNIVERSE_SCAN,
            status=ScannerRunStatus.RUNNING,
            run_started_at=local_started,
        )
        try:
            async with self._lock:
                audits: list[ScannerAuditRecord] = []
                try:
                    market_status = await self._market.status()
                    if market_status.exchange_time is None:
                        raise ScannerEvaluationError(
                            "MARKET_TIME_UNAVAILABLE", "Exchange time is unavailable"
                        )
                    exchange_time = market_status.exchange_time
                    run.run_started_at = exchange_time
                    if abs(exchange_time - local_started) > MAX_CLOCK_SKEW:
                        raise ScannerEvaluationError(
                            "CLOCK_SKEW_EXCEEDED",
                            "Local/exchange clock skew exceeds five seconds",
                        )
                    universe = await self._universe.build()
                    universe_age = exchange_time - universe.generated_at
                    if universe_age > UNIVERSE_MAX_AGE:
                        raise ScannerEvaluationError(
                            "UNIVERSE_STALE", "Universe snapshot is stale"
                        )
                except ScannerEvaluationError as exc:
                    run.status = ScannerRunStatus.FAILED
                    run.audits = [
                        ScannerAuditRecord(
                            code=exc.code,
                            detail=exc.detail,
                            timeframe=exc.timeframe,
                        )
                    ]
                    return self._complete_run(run)
                except Exception:
                    run.status = ScannerRunStatus.FAILED
                    run.audits = [
                        ScannerAuditRecord(
                            code="UNIVERSE_UNAVAILABLE",
                            detail="Required public market or Universe data is unavailable",
                        )
                    ]
                    return self._complete_run(run)

                run.universe_size = len(universe.candidates)
                discovered: list[tuple[ScannerCandidate, EvaluationContext]] = []
                data_failures = 0
                for universe_candidate in universe.candidates:
                    run.evaluated_symbols += 1
                    try:
                        candidate, symbol_audits, context = await self._evaluate_symbol(
                            universe_candidate,
                            exchange_time,
                            run.run_id,
                        )
                        audits.extend(symbol_audits)
                        run.successful_symbols += 1
                        if candidate is not None and context is not None:
                            discovered.append((candidate, context))
                            run.discovered_candidates += 1
                    except ScannerEvaluationError as exc:
                        if _is_symbol_data_failure(exc.code):
                            data_failures += 1
                        else:
                            run.successful_symbols += 1
                        audits.append(
                            ScannerAuditRecord(
                                code=exc.code,
                                detail=exc.detail,
                                symbol=universe_candidate.symbol,
                                timeframe=exc.timeframe,
                            )
                        )
                    except Exception:
                        data_failures += 1
                        audits.append(
                            ScannerAuditRecord(
                                code="INDICATOR_CALCULATION_FAILED",
                                detail="Symbol evaluation failed closed",
                                symbol=universe_candidate.symbol,
                            )
                        )

                discovered.sort(key=lambda item: _candidate_order(item[0]))
                for candidate, context in discovered:
                    if candidate.candidate_id in self._terminal_keys:
                        audits.append(
                            ScannerAuditRecord(
                                code="REENTRY_COOLDOWN_ACTIVE",
                                detail="Terminal candidate key cannot reactivate",
                                symbol=candidate.symbol,
                                direction=candidate.direction,
                                setup=candidate.setup,
                                reference_time=candidate.reference_close_time,
                            )
                        )
                        continue
                    terminal_at = self._terminal_history.get(
                        (candidate.symbol, candidate.direction, candidate.setup)
                    )
                    if terminal_at is not None and exchange_time < terminal_at + REENTRY_COOLDOWN:
                        audits.append(
                            ScannerAuditRecord(
                                code="REENTRY_COOLDOWN_ACTIVE",
                                detail="Three-candle / 45-minute re-entry cooldown is active",
                                symbol=candidate.symbol,
                                direction=candidate.direction,
                                setup=candidate.setup,
                                reference_time=candidate.reference_close_time,
                            )
                        )
                        continue

                    existing = self._candidates.get(candidate.candidate_id)
                    if existing is not None:
                        candidate.audit_codes.append("DUPLICATE_CANDIDATE_UPDATED")
                        audits.append(
                            ScannerAuditRecord(
                                code="DUPLICATE_CANDIDATE_UPDATED",
                                detail="Existing active candidate evaluation was updated",
                                symbol=candidate.symbol,
                                direction=candidate.direction,
                                setup=candidate.setup,
                                reference_time=candidate.reference_close_time,
                            )
                        )
                        run.updated_candidates += 1
                        if existing.lifecycle is CandidateLifecycle.QUALIFIED:
                            candidate.lifecycle = CandidateLifecycle.QUALIFIED
                            candidate.entry_ready = True
                            candidate.qualification_expires_at = (
                                existing.qualification_expires_at
                            )
                            candidate.score = existing.score
                            candidate.confidence = existing.confidence
                            candidate.grade = existing.grade

                    self._candidates[candidate.candidate_id] = candidate
                    if candidate.lifecycle in {
                        CandidateLifecycle.REJECTED,
                        CandidateLifecycle.INVALIDATED,
                        CandidateLifecycle.EXPIRED,
                    }:
                        self._record_terminal(candidate)
                        continue
                    self._candidate_contexts[candidate.candidate_id] = context
                    run.selected_candidates += 1
                    if candidate.lifecycle is CandidateLifecycle.QUALIFIED:
                        run.qualified_candidates += 1
                    if run.selected_candidates >= MAX_SELECTED_CANDIDATES:
                        break

                run.failed_symbols = data_failures
                if run.successful_symbols == 0:
                    run.status = ScannerRunStatus.FAILED
                    audits.append(
                        ScannerAuditRecord(
                            code="FULL_MARKET_DATA_FAILURE",
                            detail="No eligible symbol completed deterministic evaluation",
                        )
                    )
                elif data_failures:
                    run.status = ScannerRunStatus.DEGRADED
                    audits.append(
                        ScannerAuditRecord(
                            code="PARTIAL_SYMBOL_FAILURE",
                            detail=f"{data_failures} symbol evaluations failed",
                        )
                    )
                else:
                    run.status = ScannerRunStatus.COMPLETED
                run.audits = audits
                return self._complete_run(run)
        finally:
            self._run_active = False
            if self._state is ScannerState.ON:
                self._next_full_scan_at = run.run_started_at + FULL_SCAN_INTERVAL
                if self._next_refresh_at is None:
                    self._next_refresh_at = next_five_minute_boundary(run.run_started_at)

    def _complete_run(self, run: ScannerRunSummary) -> ScannerRunSummary:
        run.completed_at = self._clock.now()
        self._append_run(run)
        return run

    async def _load_context(
        self, universe: UniverseCandidate, exchange_time: datetime
    ) -> EvaluationContext:
        frames: dict[str, list[Frame]] = {}
        freshness: dict[str, Decimal] = {}
        counts: dict[str, int] = {}
        structures: dict[str, str] = {}
        for interval in ("1h", "15m", "5m"):
            candles = await self._market.candles(universe.symbol, interval, 250)
            indicators = await self._indicators.build(universe.symbol, interval, 250)
            aligned, freshness_ratio = self._engine.align(
                candles,
                indicators,
                exchange_time=exchange_time,
            )
            frames[interval] = aligned
            freshness[interval] = freshness_ratio
            counts[interval] = min(len(candles.candles), len(indicators.points))
            structures[interval] = indicators.structure.state
        direction = self._engine.regime(frames["1h"], structures["1h"])
        self._engine.volatility(frames["15m"][0], "15m")
        self._engine.volatility(frames["5m"][0], "5m")
        return EvaluationContext(
            direction=direction,
            h=frames["1h"],
            s=frames["15m"],
            e=frames["5m"],
            universe=universe,
            exchange_time=exchange_time,
            counts=counts,
            freshness=freshness,
        )

    async def _evaluate_symbol(
        self,
        universe: UniverseCandidate,
        exchange_time: datetime,
        run_id: str,
    ) -> tuple[
        ScannerCandidate | None,
        list[ScannerAuditRecord],
        EvaluationContext | None,
    ]:
        context = await self._load_context(universe, exchange_time)
        matches, setup_failures = self._engine.evaluate_setups(context)
        audits = [
            ScannerAuditRecord(
                code=failure.code,
                detail=failure.detail,
                symbol=universe.symbol,
                direction=context.direction,
                timeframe=failure.timeframe or "15m",
            )
            for failure in setup_failures
        ]
        if not matches:
            audits.append(
                ScannerAuditRecord(
                    code="SETUP_NOT_DETECTED",
                    detail="No approved deterministic setup matched",
                    symbol=universe.symbol,
                    direction=context.direction,
                    timeframe="15m",
                )
            )
            return None, audits, context

        candidates = [
            self._candidate_from_match(context, match, run_id) for match in matches
        ]
        candidates.sort(key=_candidate_order)
        selected = candidates[0]
        for item in candidates[1:]:
            audits.append(
                ScannerAuditRecord(
                    code="SUPERSEDED_BY_HIGHER_RANKED_SETUP",
                    detail="Valid setup retained as audit evidence but not selected",
                    symbol=item.symbol,
                    direction=item.direction,
                    setup=item.setup,
                    reference_time=item.reference_close_time,
                )
            )
        return selected, audits, context

    def _candidate_from_match(
        self,
        context: EvaluationContext,
        match: SetupMatch,
        run_id: str,
    ) -> ScannerCandidate:
        entry_ready = (
            context.e[0].candle.close_time > match.setup_confirmed_at
            and context.exchange_time < match.expires_at
            and self._engine.shared_entry(
                context.e,
                context.direction,
                match.entry_trigger_price,
            )
        )
        score, confidence, grade, components = self._engine.score(
            context,
            match,
            entry_ready,
        )
        audit_codes: list[str] = []
        if confidence < 60:
            lifecycle = CandidateLifecycle.REJECTED
            audit_codes.append("CONFIDENCE_BELOW_60")
        elif score < 80:
            lifecycle = CandidateLifecycle.REJECTED
            audit_codes.append("SCORE_BELOW_80")
        elif (
            entry_ready
            and score >= 85
            and confidence >= 70
            and grade in {ScannerGrade.A, ScannerGrade.A_PLUS}
        ):
            lifecycle = CandidateLifecycle.QUALIFIED
        else:
            lifecycle = CandidateLifecycle.WATCH_NEAR
            if not entry_ready:
                audit_codes.append("ENTRY_NOT_READY")
                e0 = context.e[0]
                extension = abs(
                    _directional_ema_extension(
                        e0.candle.close,
                        _frame_value(e0, "ema20"),
                        context.direction,
                    )
                )
                if extension > Decimal("0.75") * _frame_value(e0, "atr14"):
                    audit_codes.append("ENTRY_OVEREXTENDED")
            if grade is ScannerGrade.B_PLUS:
                audit_codes.append("GRADE_B_PLUS_WATCH_ONLY")
            if 60 <= confidence <= 69:
                audit_codes.append("CONFIDENCE_WATCH_ONLY")

        evidence = dict(match.evidence)
        evidence["source_run_id"] = run_id
        return ScannerCandidate(
            candidate_id=_candidate_key(
                context.universe.symbol,
                context.direction,
                match.setup,
                match.reference_close_time,
            ),
            symbol=context.universe.symbol,
            direction=context.direction,
            setup=match.setup,
            setup_name=SETUP_NAMES[match.setup],
            reference_close_time=match.reference_close_time,
            setup_confirmed_at=match.setup_confirmed_at,
            expires_at=match.expires_at,
            qualification_expires_at=(
                context.e[0].candle.close_time + QUALIFICATION_EXPIRY
                if lifecycle is CandidateLifecycle.QUALIFIED
                else None
            ),
            lifecycle=lifecycle,
            score=score,
            confidence=confidence,
            grade=grade,
            entry_ready=entry_ready,
            universe_rank=context.universe.rank,
            quote_volume=context.universe.quote_volume,
            spread_bps=context.universe.spread_bps,
            level=match.level,
            selected_ema=match.selected_ema,
            entry_trigger_price=match.entry_trigger_price,
            evaluated_at=context.exchange_time,
            accepted_reasons=list(match.accepted_reasons),
            audit_codes=audit_codes,
            evidence=evidence,
            score_components=components,
        )
