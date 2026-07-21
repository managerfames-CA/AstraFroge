"""Phase 3 regression tests for Scanner/Strategy responsibility separation."""

from __future__ import annotations

from typing import Any, cast

from app.api.v1 import dependencies
from app.scanner.constants import SCANNER_MAX_SYMBOLS
from app.services.scanner_base import EvaluationContext
from app.services.scanner_opportunity import OpportunityScannerEngine, OpportunityScannerService
from app.services.scanner_strategy_separated import StrategySeparatedScannerService
from app.services.strategy_evaluation import StrategyEvaluationService


class _FakeStrategyEngine:
    def __init__(self) -> None:
        self.calls = 0
        self.context: object | None = None

    def evaluate_setups(self, context: EvaluationContext) -> tuple[list[Any], list[Any]]:
        self.calls += 1
        self.context = context
        return [], []


def test_strategy_evaluation_service_is_the_setup_rule_boundary() -> None:
    engine = _FakeStrategyEngine()
    service = StrategyEvaluationService(cast(Any, engine))
    context = cast(EvaluationContext, object())

    result = service.evaluate(context)

    assert result.matches == ()
    assert result.failures == ()
    assert engine.calls == 1
    assert engine.context is context


def test_runtime_factory_uses_strategy_separated_scanner() -> None:
    assert dependencies.ScannerService is StrategySeparatedScannerService
    assert issubclass(StrategySeparatedScannerService, OpportunityScannerService)


def test_opportunity_policy_is_preserved_inside_strategy_boundary() -> None:
    engine = OpportunityScannerEngine()
    service = StrategyEvaluationService(engine)

    assert service._engine is engine


def test_directional_shortlist_is_capped_at_twenty() -> None:
    assert SCANNER_MAX_SYMBOLS == 20
