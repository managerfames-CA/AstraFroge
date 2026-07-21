"""Dedicated deterministic strategy-evaluation boundary for Phase 3.

Scanner owns symbol discovery/shortlisting. This service owns setup evaluation for the
approved deterministic strategies. Signal scoring/qualification remains unchanged until
Phase 4.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.scanner_base import EvaluationContext, ScannerEvaluationError, SetupMatch
from app.services.scanner_setups import ScannerSetupEngine


@dataclass(frozen=True)
class StrategyEvaluationResult:
    """Raw deterministic setup evaluation result for one shortlisted symbol."""

    matches: tuple[SetupMatch, ...]
    failures: tuple[ScannerEvaluationError, ...]


class StrategyEvaluationService:
    """Evaluate the approved strategy set without owning Scanner lifecycle or execution."""

    def __init__(self, engine: ScannerSetupEngine) -> None:
        self._engine = engine

    def evaluate(self, context: EvaluationContext) -> StrategyEvaluationResult:
        matches, failures = self._engine.evaluate_setups(context)
        return StrategyEvaluationResult(matches=tuple(matches), failures=tuple(failures))
