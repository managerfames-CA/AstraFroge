"""Indicator Engine API route."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.v1.dependencies import get_indicator_service
from app.integrations.binance.public_client import BinancePublicClientError
from app.schemas.indicators import IndicatorSeries
from app.services.indicators import IndicatorService

router = APIRouter(prefix="/indicators", tags=["indicators"])


@router.get("/{symbol}", response_model=IndicatorSeries)
async def indicator_series(
    symbol: str,
    service: IndicatorService = Depends(get_indicator_service),  # noqa: B008
    interval: Annotated[Literal["5m", "15m", "1h"], Query()] = "15m",
    limit: Annotated[int, Query(ge=1, le=1000)] = 250,
) -> IndicatorSeries:
    """Return deterministic indicators aligned to closed candles."""

    normalized = symbol.strip().upper()
    if not normalized or not normalized.isalnum():
        raise HTTPException(status_code=422, detail="Invalid symbol")
    try:
        return await service.build(normalized, interval, limit)
    except BinancePublicClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Invalid indicator input") from exc
