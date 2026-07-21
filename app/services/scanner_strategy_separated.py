"""Phase 3 Scanner/Strategy separation with Phase 4 fact-only candidate output."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.schemas.scanner import (
    CandidateLifecycle,
    ScannerAuditRecord,
    ScannerCandidate,
    ScannerRunSummary,
)
from app.schemas.universe import UniverseCandidate
from app.services.scanner_base import EvaluationContext, Frame, SetupMatch
from app.services.scanner_opportunity import OpportunityScannerEngine, OpportunityScannerService
from app.services.strategy_evaluation import StrategyEvaluationService


class StrategySeparatedScannerService(OpportunityScannerService):
    """Discover setups and report facts while delegating final eligibility to Phase 4."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        engine = OpportunityScannerEngine()
        self._engine = engine
        self._strategy_evaluation = StrategyEvaluationService(engine)
        self._context_provenance: dict[int, dict[str, Any]] = {}
        self._refresh_provenance: dict[str, dict[str, Any]] = {}

    async def _load_context(
        self,
        universe: UniverseCandidate,
        exchange_time: datetime,
    ) -> EvaluationContext:
        frames: dict[str, list[Frame]] = {}
        freshness: dict[str, Decimal] = {}
        counts: dict[str, int] = {}
        structures: dict[str, str] = {}
        versions: dict[str, str] = {}
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
            if candles.snapshot_version:
                versions[f"{interval}:candles"] = candles.snapshot_version
            if indicators.snapshot_version:
                versions[f"{interval}:indicators"] = indicators.snapshot_version
        direction = self._engine.regime(frames["1h"], structures["1h"])
        self._engine.volatility(frames["15m"][0], "15m")
        self._engine.volatility(frames["5m"][0], "5m")
        context = EvaluationContext(
            direction=direction,
            h=frames["1h"],
            s=frames["15m"],
            e=frames["5m"],
            universe=universe,
            exchange_time=exchange_time,
            counts=counts,
            freshness=freshness,
        )
        self._context_provenance[id(context)] = self._provenance(
            versions,
            frames["5m"][0].candle.close_time,
        )
        return context

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
        versions: dict[str, str] = {}
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
            if candles.snapshot_version:
                versions[f"{interval}:candles"] = candles.snapshot_version
            if indicators.snapshot_version:
                versions[f"{interval}:indicators"] = indicators.snapshot_version
            if interval == "1h":
                structure = indicators.structure.state
        self._refresh_provenance[symbol] = self._provenance(
            versions,
            frames["5m"][0].candle.close_time,
        )
        return (
            frames["1h"],
            frames["15m"],
            frames["5m"],
            counts,
            freshness,
            structure,
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
        result = self._strategy_evaluation.evaluate(context)
        audits = [
            ScannerAuditRecord(
                code=failure.code,
                detail=failure.detail,
                symbol=universe.symbol,
                direction=context.direction,
                timeframe=failure.timeframe or "15m",
            )
            for failure in result.failures
        ]
        if not result.matches:
            audits.append(
                ScannerAuditRecord(
                    code="SETUP_NOT_DETECTED",
                    detail="No approved deterministic strategy matched",
                    symbol=universe.symbol,
                    direction=context.direction,
                    timeframe="15m",
                )
            )
            return None, audits, context

        candidates = [
            self._candidate_from_match(context, match, run_id) for match in result.matches
        ]
        failure_codes = list(dict.fromkeys(failure.code for failure in result.failures))
        for candidate in candidates:
            candidate.evidence["strategy_reason_codes"] = failure_codes
        candidates.sort(key=self._candidate_order_key)
        selected = candidates[0]
        for item in candidates[1:]:
            audits.append(
                ScannerAuditRecord(
                    code="SUPERSEDED_BY_HIGHER_RANKED_SETUP",
                    detail="Valid strategy match retained as audit evidence but not selected",
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
        candidate = super()._candidate_from_match(context, match, run_id)
        candidate.evidence["legacy_scanner_lifecycle"] = candidate.lifecycle.value
        provenance = self._context_provenance.get(id(context), {})
        self._apply_provenance(candidate, provenance)
        candidate.lifecycle = CandidateLifecycle.DETECTED
        candidate.qualification_expires_at = None
        return candidate

    async def active_refresh(self) -> ScannerRunSummary:
        run = await super().active_refresh()
        for candidate in self._candidates.values():
            provenance = self._refresh_provenance.get(candidate.symbol)
            if provenance is not None:
                self._apply_provenance(candidate, provenance)
            if candidate.lifecycle in {
                CandidateLifecycle.QUALIFIED,
                CandidateLifecycle.WATCH_NEAR,
            }:
                candidate.evidence["legacy_scanner_lifecycle"] = candidate.lifecycle.value
                candidate.lifecycle = CandidateLifecycle.DETECTED
                candidate.qualification_expires_at = None
        run.qualified_candidates = 0
        return run

    def _terminal(
        self,
        candidate: ScannerCandidate,
        state: CandidateLifecycle,
        code: str,
        evaluated_at: datetime,
    ) -> None:
        if code in {"CONFIDENCE_BELOW_60", "SCORE_BELOW_80"}:
            candidate.lifecycle = CandidateLifecycle.DETECTED
            if code not in candidate.audit_codes:
                candidate.audit_codes.append(code)
            candidate.evaluated_at = evaluated_at
            candidate.qualification_expires_at = None
            return
        super()._terminal(candidate, state, code, evaluated_at)

    @staticmethod
    def _provenance(
        versions: dict[str, str],
        entry_close_time: datetime,
    ) -> dict[str, Any]:
        required = {
            f"{interval}:{kind}"
            for interval in ("1h", "15m", "5m")
            for kind in ("candles", "indicators")
        }
        payload: dict[str, Any] = {
            "source_snapshot_versions": dict(sorted(versions.items())),
            "entry_snapshot_close_time": entry_close_time.isoformat(),
        }
        if required.issubset(versions):
            encoded = json.dumps(versions, sort_keys=True, separators=(",", ":")).encode()
            payload["source_snapshot_version"] = hashlib.sha256(encoded).hexdigest()
        return payload

    @staticmethod
    def _apply_provenance(
        candidate: ScannerCandidate,
        provenance: dict[str, Any],
    ) -> None:
        for key in (
            "source_snapshot_versions",
            "source_snapshot_version",
            "entry_snapshot_close_time",
        ):
            if key in provenance:
                candidate.evidence[key] = provenance[key]

    @staticmethod
    def _candidate_order_key(candidate: ScannerCandidate) -> tuple[object, ...]:
        return (
            -(candidate.score if candidate.score is not None else -1),
            -(candidate.confidence if candidate.confidence is not None else -1),
            candidate.universe_rank,
            candidate.spread_bps,
            -candidate.quote_volume,
            candidate.symbol,
            candidate.setup.value,
            candidate.reference_close_time,
        )
