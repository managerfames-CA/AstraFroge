"""Active Candidate Refresh implementation and public Scanner service."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from app.schemas.scanner import (
    CandidateLifecycle,
    ScannerAuditRecord,
    ScannerCandidate,
    ScannerGrade,
    ScannerRunStatus,
    ScannerRunSummary,
    ScannerRunType,
)
from app.services.scanner_base import (
    EvaluationContext,
    Frame,
    ScannerEvaluationError,
    SetupMatch,
    _directional_ema_extension,
    _frame_value,
)
from app.services.scanner_contract import MAX_CLOCK_SKEW, QUALIFICATION_EXPIRY
from app.services.scanner_full import ScannerFullService

_TREND_INVALIDATION_CODES = {
    "TREND_SIDEWAYS",
    "TREND_MIXED",
    "TREND_DIRECTION_MISMATCH",
}


class ScannerService(ScannerFullService):
    """Complete deterministic Scanner Engine Runtime."""

    async def active_refresh(self) -> ScannerRunSummary:
        if self._run_active or self._lock.locked():
            return await self._skipped(ScannerRunType.ACTIVE_CANDIDATE_REFRESH)
        self._run_active = True
        local_started = self._clock.now()
        run = ScannerRunSummary(
            run_id=str(uuid4()),
            run_type=ScannerRunType.ACTIVE_CANDIDATE_REFRESH,
            status=ScannerRunStatus.RUNNING,
            run_started_at=local_started,
        )
        try:
            async with self._lock:
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
                except ScannerEvaluationError as exc:
                    run.status = ScannerRunStatus.FAILED
                    run.audits = [ScannerAuditRecord(code=exc.code, detail=exc.detail)]
                    return self._complete_run(run)
                except Exception:
                    run.status = ScannerRunStatus.FAILED
                    run.audits = [
                        ScannerAuditRecord(
                            code="MARKET_TIME_UNAVAILABLE",
                            detail="Exchange time is unavailable",
                        )
                    ]
                    return self._complete_run(run)

                audits: list[ScannerAuditRecord] = []
                failures = 0
                active = [
                    candidate
                    for candidate in self._candidates.values()
                    if candidate.lifecycle
                    in {
                        CandidateLifecycle.DETECTED,
                        CandidateLifecycle.WATCH_NEAR,
                        CandidateLifecycle.QUALIFIED,
                    }
                ]
                run.evaluated_symbols = len(active)
                for candidate in active:
                    try:
                        current_h, current_s, current_e, counts, freshness, structure = (
                            await self._load_refresh_inputs(candidate.symbol, exchange_time)
                        )
                        current_direction = self._engine.regime(current_h, structure)
                        if current_direction is not candidate.direction:
                            raise ScannerEvaluationError(
                                "TREND_DIRECTION_MISMATCH",
                                "Current 1H regime conflicts with candidate direction",
                                "1h",
                            )
                        self._engine.volatility(current_s[0], "15m")
                        self._engine.volatility(current_e[0], "5m")
                        if self._engine.invalidated(candidate, current_s[0], current_e[0]):
                            self._terminal(
                                candidate,
                                CandidateLifecycle.INVALIDATED,
                                "CANDIDATE_INVALIDATED",
                                exchange_time,
                            )
                            run.successful_symbols += 1
                            continue

                        expiry = (
                            candidate.qualification_expires_at
                            if candidate.lifecycle is CandidateLifecycle.QUALIFIED
                            else candidate.expires_at
                        )
                        if expiry is not None and exchange_time >= expiry:
                            self._terminal(
                                candidate,
                                CandidateLifecycle.EXPIRED,
                                "CANDIDATE_EXPIRED",
                                exchange_time,
                            )
                            run.successful_symbols += 1
                            continue

                        if candidate.lifecycle is CandidateLifecycle.QUALIFIED:
                            candidate.evaluated_at = exchange_time
                            run.successful_symbols += 1
                            run.updated_candidates += 1
                            continue

                        stored = self._candidate_contexts.get(candidate.candidate_id)
                        if stored is None:
                            raise ScannerEvaluationError(
                                "INDICATOR_CALCULATION_FAILED",
                                "Stored setup context is unavailable",
                            )
                        context = EvaluationContext(
                            direction=candidate.direction,
                            h=current_h,
                            s=stored.s,
                            e=current_e,
                            universe=stored.universe,
                            exchange_time=exchange_time,
                            counts=counts,
                            freshness=freshness,
                        )
                        match = self._stored_match(candidate)
                        entry_ready = (
                            current_e[0].candle.close_time > candidate.setup_confirmed_at
                            and exchange_time < candidate.expires_at
                            and self._engine.shared_entry(
                                current_e,
                                candidate.direction,
                                candidate.entry_trigger_price,
                            )
                        )
                        score, confidence, grade, components = self._engine.score(
                            context,
                            match,
                            entry_ready,
                        )
                        candidate.score = score
                        candidate.confidence = confidence
                        candidate.grade = grade
                        candidate.entry_ready = entry_ready
                        candidate.score_components = components
                        candidate.evaluated_at = exchange_time
                        candidate.audit_codes = []

                        if confidence < 60:
                            self._terminal(
                                candidate,
                                CandidateLifecycle.REJECTED,
                                "CONFIDENCE_BELOW_60",
                                exchange_time,
                            )
                        elif score < 80:
                            self._terminal(
                                candidate,
                                CandidateLifecycle.REJECTED,
                                "SCORE_BELOW_80",
                                exchange_time,
                            )
                        elif (
                            entry_ready
                            and score >= 85
                            and confidence >= 70
                            and grade in {ScannerGrade.A, ScannerGrade.A_PLUS}
                        ):
                            candidate.lifecycle = CandidateLifecycle.QUALIFIED
                            candidate.qualification_expires_at = (
                                current_e[0].candle.close_time + QUALIFICATION_EXPIRY
                            )
                            run.qualified_candidates += 1
                        else:
                            candidate.lifecycle = CandidateLifecycle.WATCH_NEAR
                            self._set_watch_reasons(candidate, current_e[0])
                        run.successful_symbols += 1
                        run.updated_candidates += 1
                    except ScannerEvaluationError as exc:
                        if exc.code in _TREND_INVALIDATION_CODES:
                            self._terminal(
                                candidate,
                                CandidateLifecycle.INVALIDATED,
                                "CANDIDATE_INVALIDATED",
                                exchange_time,
                            )
                            audits.append(
                                ScannerAuditRecord(
                                    code=exc.code,
                                    detail=exc.detail,
                                    symbol=candidate.symbol,
                                    direction=candidate.direction,
                                    setup=candidate.setup,
                                    timeframe=exc.timeframe,
                                )
                            )
                            run.successful_symbols += 1
                            continue
                        if self._expiry_reached(candidate, exchange_time):
                            self._terminal(
                                candidate,
                                CandidateLifecycle.EXPIRED,
                                "CANDIDATE_EXPIRED",
                                exchange_time,
                            )
                            run.successful_symbols += 1
                            continue
                        failures += 1
                        audits.append(
                            ScannerAuditRecord(
                                code=exc.code,
                                detail=exc.detail,
                                symbol=candidate.symbol,
                                direction=candidate.direction,
                                setup=candidate.setup,
                                timeframe=exc.timeframe,
                            )
                        )
                    except Exception:
                        if self._expiry_reached(candidate, exchange_time):
                            self._terminal(
                                candidate,
                                CandidateLifecycle.EXPIRED,
                                "CANDIDATE_EXPIRED",
                                exchange_time,
                            )
                            run.successful_symbols += 1
                            continue
                        failures += 1
                        audits.append(
                            ScannerAuditRecord(
                                code="INDICATOR_CALCULATION_FAILED",
                                detail="Active refresh failed closed; prior state preserved",
                                symbol=candidate.symbol,
                                direction=candidate.direction,
                                setup=candidate.setup,
                            )
                        )

                run.failed_symbols = failures
                if active and run.successful_symbols == 0 and failures:
                    run.status = ScannerRunStatus.FAILED
                elif failures:
                    run.status = ScannerRunStatus.DEGRADED
                    audits.append(
                        ScannerAuditRecord(
                            code="PARTIAL_SYMBOL_FAILURE",
                            detail=f"{failures} active candidate refreshes failed",
                        )
                    )
                else:
                    run.status = ScannerRunStatus.COMPLETED
                run.audits = audits
                return self._complete_run(run)
        finally:
            self._run_active = False

    async def _load_refresh_inputs(
        self,
        symbol: str,
        exchange_time: datetime,
    ) -> tuple[
        list[Frame],
        list[Frame],
        list[Frame],
        dict[str, int],
        dict[str, Decimal],
        str,
    ]:
        frames: dict[str, list[Frame]] = {}
        counts: dict[str, int] = {}
        freshness: dict[str, Decimal] = {}
        structure = "insufficient_data"
        for interval in ("1h", "15m", "5m"):
            candles = await self._market.candles(symbol, interval, 250)
            indicators = await self._indicators.build(symbol, interval, 250)
            aligned, ratio = self._engine.align(
                candles,
                indicators,
                exchange_time=exchange_time,
            )
            frames[interval] = aligned
            counts[interval] = min(len(candles.candles), len(indicators.points))
            freshness[interval] = ratio
            if interval == "1h":
                structure = indicators.structure.state
        return (
            frames["1h"],
            frames["15m"],
            frames["5m"],
            counts,
            freshness,
            structure,
        )

    @staticmethod
    def _stored_match(candidate: ScannerCandidate) -> SetupMatch:
        setup_points = candidate.score_components.get("setup")
        if setup_points is None:
            raise ScannerEvaluationError(
                "INDICATOR_CALCULATION_FAILED",
                "Stored setup score component is unavailable",
            )
        return SetupMatch(
            setup=candidate.setup,
            reference_close_time=candidate.reference_close_time,
            setup_confirmed_at=candidate.setup_confirmed_at,
            expires_at=candidate.expires_at,
            level=candidate.level,
            selected_ema=candidate.selected_ema,
            entry_trigger_price=candidate.entry_trigger_price,
            setup_points=setup_points,
            accepted_reasons=tuple(candidate.accepted_reasons),
            evidence=dict(candidate.evidence),
        )

    def _set_watch_reasons(self, candidate: ScannerCandidate, e0: Frame) -> None:
        if not candidate.entry_ready:
            candidate.audit_codes.append("ENTRY_NOT_READY")
            extension = abs(
                _directional_ema_extension(
                    e0.candle.close,
                    _frame_value(e0, "ema20"),
                    candidate.direction,
                )
            )
            if extension > Decimal("0.75") * _frame_value(e0, "atr14"):
                candidate.audit_codes.append("ENTRY_OVEREXTENDED")
        if candidate.grade is ScannerGrade.B_PLUS:
            candidate.audit_codes.append("GRADE_B_PLUS_WATCH_ONLY")
        if candidate.confidence is not None and 60 <= candidate.confidence <= 69:
            candidate.audit_codes.append("CONFIDENCE_WATCH_ONLY")

    @staticmethod
    def _expiry_reached(candidate: ScannerCandidate, exchange_time: datetime) -> bool:
        expiry = (
            candidate.qualification_expires_at
            if candidate.lifecycle is CandidateLifecycle.QUALIFIED
            else candidate.expires_at
        )
        return expiry is not None and exchange_time >= expiry

    def _terminal(
        self,
        candidate: ScannerCandidate,
        state: CandidateLifecycle,
        code: str,
        evaluated_at: datetime,
    ) -> None:
        candidate.lifecycle = state
        if code not in candidate.audit_codes:
            candidate.audit_codes.append(code)
        candidate.evaluated_at = evaluated_at
        self._record_terminal(candidate)
