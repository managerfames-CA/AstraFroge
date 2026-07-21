"""Public market-data API routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.v1.dependencies import get_market_service
from app.integrations.binance.public_client import BinancePublicClientError
from app.schemas.market import MarketCandleSeries, MarketStatus, MarketSymbol, MarketTicker
from app.services.market_data import MarketDataService

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/status", response_model=MarketStatus)
async def market_status(
    service: MarketDataService = Depends(get_market_service),  # noqa: B008
) -> MarketStatus:
    try:
        return await service.status()
    except BinancePublicClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/symbols", response_model=list[MarketSymbol])
async def market_symbols(
    service: MarketDataService = Depends(get_market_service),  # noqa: B008
) -> list[MarketSymbol]:
    try:
        return await service.symbols()
    except (BinancePublicClientError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/ticker/{symbol}", response_model=MarketTicker)
async def market_ticker(
    symbol: str,
    service: MarketDataService = Depends(get_market_service),  # noqa: B008
) -> MarketTicker:
    normalized = symbol.strip().upper()
    if not normalized or not normalized.isalnum():
        raise HTTPException(status_code=422, detail="Invalid symbol")
    try:
        return await service.ticker(normalized)
    except BinancePublicClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Invalid ticker payload") from exc


@router.get("/klines/{symbol}", response_model=MarketCandleSeries)
async def market_klines(
    symbol: str,
    service: MarketDataService = Depends(get_market_service),  # noqa: B008
    interval: Annotated[str, Query()] = "15m",
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> MarketCandleSeries:
    normalized = symbol.strip().upper()
    if not normalized or not normalized.isalnum():
        raise HTTPException(status_code=422, detail="Invalid symbol")
    try:
        return await service.candles(normalized, interval, limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except BinancePublicClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
