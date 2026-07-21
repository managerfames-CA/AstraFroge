"""Universe Engine API integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.api.v1.dependencies import get_universe_service
from app.core.config import Settings
from app.integrations.binance.public_client import BinancePublicClientError
from app.main import create_app
from app.schemas.universe import UniverseCandidate, UniverseSnapshot


class FakeUniverseService:
    def __init__(self, failure: Exception | None = None) -> None:
        self._failure = failure

    async def build(self) -> UniverseSnapshot:
        if self._failure is not None:
            raise self._failure
        return UniverseSnapshot(
            generated_at=datetime.now(UTC),
            max_symbols=50,
            min_quote_volume=Decimal("10000000"),
            max_spread_bps=Decimal("10"),
            eligible_count=1,
            rejected_count=0,
            candidates=[
                UniverseCandidate(
                    rank=1,
                    symbol="BTCUSDT",
                    base_asset="BTC",
                    quote_volume=Decimal("50000000"),
                    bid_price=Decimal("100"),
                    ask_price=Decimal("100.01"),
                    spread_bps=Decimal("0.99995"),
                )
            ],
            rejections=[],
        )


async def _request(service: FakeUniverseService) -> httpx.Response:
    application = create_app(Settings(environment="test"))
    application.dependency_overrides[get_universe_service] = lambda: service
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/api/v1/universe")


@pytest.mark.anyio
async def test_universe_endpoint_returns_ranked_snapshot() -> None:
    response = await _request(FakeUniverseService())

    assert response.status_code == 200
    payload = response.json()
    assert payload["eligible_count"] == 1
    assert payload["candidates"][0]["symbol"] == "BTCUSDT"
    assert payload["candidates"][0]["rank"] == 1


@pytest.mark.anyio
async def test_universe_upstream_failure_maps_to_503() -> None:
    response = await _request(
        FakeUniverseService(BinancePublicClientError("market data unavailable"))
    )

    assert response.status_code == 503


@pytest.mark.anyio
async def test_universe_payload_failure_maps_to_502() -> None:
    response = await _request(FakeUniverseService(ValueError("invalid symbols payload")))

    assert response.status_code == 502
