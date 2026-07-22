"""Directional Scanner universe prefilter regression tests."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, cast

import pytest

from app.schemas.scanner import ScannerDirection
from app.schemas.universe import UniverseCandidate, UniverseRejection, UniverseSnapshot
from app.services.scanner_base import ScannerEvaluationError
from app.services.scanner_universe import DirectionalScannerUniverse
from tests.unit.scanner_test_support import NOW, FakeMarket, universe


class FakeBroadUniverse:
    def __init__(self, candidates: list[UniverseCandidate]) -> None:
        self.candidates = candidates
        self.calls = 0

    async def build(self) -> UniverseSnapshot:
        self.calls += 1
        return UniverseSnapshot(
            generated_at=NOW,
            max_symbols=120,
            min_quote_volume=Decimal("10000000"),
            max_spread_bps=Decimal("10"),
            eligible_count=len(self.candidates),
            rejected_count=0,
            candidates=self.candidates,
            rejections=[],
        )


class StubDirectionalUniverse(DirectionalScannerUniverse):
    async def _classify_candidate(
        self,
        candidate: UniverseCandidate,
        exchange_time: datetime,
    ) -> tuple[UniverseCandidate | None, UniverseRejection | None]:
        del exchange_time
        if candidate.rank % 4 == 0:
            return None, UniverseRejection(
                symbol=candidate.symbol,
                code="trend_sideways",
                detail="1H prefilter: sideways",
            )
        return candidate, None


@pytest.mark.anyio
async def test_prefilter_accepts_directional_1h_regime() -> None:
    item = universe()
    service = DirectionalScannerUniverse(FakeBroadUniverse([item]), FakeMarket())
    cast(Any, service._scanner_engine).regime = lambda frames, structure: ScannerDirection.LONG

    accepted, rejection = await service._classify_candidate(item, NOW)

    assert accepted == item
    assert rejection is None


@pytest.mark.anyio
async def test_prefilter_rejects_sideways_1h_regime() -> None:
    item = universe()
    service = DirectionalScannerUniverse(FakeBroadUniverse([item]), FakeMarket())

    def sideways(_frames: object, _structure: str) -> ScannerDirection:
        raise ScannerEvaluationError("TREND_SIDEWAYS", "1H regime is SIDEWAYS", "1h")

    service._scanner_engine.regime = sideways  # type: ignore[assignment]

    accepted, rejection = await service._classify_candidate(item, NOW)

    assert accepted is None
    assert rejection is not None
    assert rejection.code == "trend_sideways"


@pytest.mark.anyio
async def test_broad_pool_filters_then_caps_final_50_and_caches() -> None:
    candidates = [
        universe().model_copy(
            update={
                "rank": index,
                "symbol": f"S{index:03d}USDT",
                "base_asset": f"S{index:03d}",
            }
        )
        for index in range(1, 81)
    ]
    broad = FakeBroadUniverse(candidates)
    service = StubDirectionalUniverse(broad, FakeMarket(), max_symbols=50, concurrency=4)

    first = await service.build()
    second = await service.build()

    assert broad.calls == 1
    assert len(first.candidates) == 50
    assert first.eligible_count == 50
    assert [item.rank for item in first.candidates] == list(range(1, 51))
    assert all(int(item.base_asset[1:]) % 4 != 0 for item in first.candidates)
    assert any(item.code == "trend_sideways" for item in first.rejections)
    assert any(item.code == "directional_universe_limit" for item in first.rejections)
    assert [item.symbol for item in second.candidates] == [item.symbol for item in first.candidates]
