"""Scanner-specific broad-universe prefilter for directional intraday opportunity selection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Protocol

from app.scanner.constants import UNIVERSE_REFRESH_INTERVAL
from app.schemas.scanner import ScannerDirection
from app.schemas.universe import (
    UniverseCandidate,
    UniverseRejection,
    UniverseRejectionCode,
    UniverseSnapshot,
)
from app.services.indicators import IndicatorEngine
from app.services.scanner_base import ScannerEvaluationError
from app.services.scanner_contract import MAX_SELECTED_CANDIDATES
from app.services.scanner_runtime import ScannerMarketProvider
from app.services.scanner_scoring import ScannerEngine


class BroadUniverseProvider(Protocol):
    """Broad candidate source required by the Scanner-specific prefilter."""

    async def build(self) -> UniverseSnapshot: ...


class DirectionalScannerUniverse:
    """Build a broad pool, remove non-directional 1H regimes, then cap the final scan list."""

    def __init__(
        self,
        source: BroadUniverseProvider,
        market_service: ScannerMarketProvider,
        *,
        max_symbols: int = MAX_SELECTED_CANDIDATES,
        concurrency: int = 8,
    ) -> None:
        self._source = source
        self._market = market_service
        self._max_symbols = max_symbols
        self._concurrency = max(1, concurrency)
        self._indicator_engine = IndicatorEngine()
        self._scanner_engine = ScannerEngine()
        self._cached_snapshot: UniverseSnapshot | None = None
        self._cached_at: datetime | None = None

    async def _classify_candidate(
        self,
        candidate: UniverseCandidate,
        exchange_time: datetime,
    ) -> tuple[UniverseCandidate | None, UniverseRejection | None]:
        try:
            candles = await self._market.candles(candidate.symbol, "1h", 250)
            indicators = self._indicator_engine.calculate(candles)
            frames, _ = self._scanner_engine.align(
                candles,
                indicators,
                exchange_time=exchange_time,
            )
            direction = self._scanner_engine.regime(frames, indicators.structure.state)
            if direction not in {ScannerDirection.LONG, ScannerDirection.SHORT}:
                return None, UniverseRejection(
                    symbol=candidate.symbol,
                    code="trend_not_directional",
                    detail="1H regime is not directionally tradable",
                )
            return candidate, None
        except ScannerEvaluationError as exc:
            code: UniverseRejectionCode = (
                "trend_sideways"
                if exc.code == "TREND_SIDEWAYS"
                else "trend_mixed"
                if exc.code == "TREND_MIXED"
                else "trend_prefilter_failed"
            )
            return None, UniverseRejection(
                symbol=candidate.symbol,
                code=code,
                detail=f"1H prefilter {exc.code}: {exc.detail}",
            )
        except Exception:
            return None, UniverseRejection(
                symbol=candidate.symbol,
                code="trend_prefilter_failed",
                detail="1H directional prefilter failed closed",
            )

    async def build(self) -> UniverseSnapshot:
        now = datetime.now(UTC)
        if (
            self._cached_snapshot is not None
            and self._cached_at is not None
            and now - self._cached_at < UNIVERSE_REFRESH_INTERVAL
        ):
            return self._cached_snapshot.model_copy(update={"generated_at": now})

        broad = await self._source.build()
        semaphore = asyncio.Semaphore(self._concurrency)

        async def classify(
            candidate: UniverseCandidate,
        ) -> tuple[UniverseCandidate | None, UniverseRejection | None]:
            async with semaphore:
                return await self._classify_candidate(candidate, now)

        classified = await asyncio.gather(*(classify(item) for item in broad.candidates))
        directional = [candidate for candidate, _ in classified if candidate is not None]
        prefilter_rejections = [rejection for _, rejection in classified if rejection is not None]

        selected = directional[: self._max_symbols]
        selected_symbols = {item.symbol for item in selected}
        overflow_rejections = [
            UniverseRejection(
                symbol=item.symbol,
                code="directional_universe_limit",
                detail=(
                    "Directional rank is outside the final scanner limit of "
                    f"{self._max_symbols}"
                ),
            )
            for item in directional
            if item.symbol not in selected_symbols
        ]
        reranked = [
            item.model_copy(update={"rank": rank})
            for rank, item in enumerate(selected, start=1)
        ]
        all_rejections = sorted(
            [*broad.rejections, *prefilter_rejections, *overflow_rejections],
            key=lambda item: (item.symbol, item.code),
        )
        snapshot = UniverseSnapshot(
            generated_at=now,
            max_symbols=self._max_symbols,
            min_quote_volume=broad.min_quote_volume,
            max_spread_bps=broad.max_spread_bps,
            eligible_count=len(reranked),
            rejected_count=len(all_rejections),
            candidates=reranked,
            rejections=all_rejections,
        )
        self._cached_snapshot = snapshot
        self._cached_at = now
        return snapshot
