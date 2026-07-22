"""Trade Management Engine API routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.api.v1.active_trade_dependencies import get_active_trade_authority_service
from app.api.v1.manual_close_dependencies import (
    get_durable_trade_management_service,
)
from app.core.security import MutationAuthorization, authorize_mutation
from app.schemas.execution import DemoTradeRecord
from app.schemas.scanner import ScannerDirection, ScannerGrade
from app.schemas.trade_management import (
    ManagedTradeRecordList,
    TradeCloseRequest,
    TradeListFilters,
    TradeManagementStatusResponse,
    TradeSortBy,
)
from app.services.active_trade_authority import ActiveTradeAuthorityService
from app.services.durable_trade_management import DurableTradeManagementService

router = APIRouter(prefix="/trade-management", tags=["trade-management"])


@router.get("/status", response_model=TradeManagementStatusResponse)
async def trade_management_status(
    service: ActiveTradeAuthorityService = Depends(  # noqa: B008
        get_active_trade_authority_service
    ),
) -> TradeManagementStatusResponse:
    """Return exchange-authoritative Active Trades readiness and summary counts."""

    return service.status()


@router.get("/trades", response_model=ManagedTradeRecordList)
async def tracked_trades(
    service: ActiveTradeAuthorityService = Depends(  # noqa: B008
        get_active_trade_authority_service
    ),
    symbol: Annotated[str | None, Query()] = None,
    direction: Annotated[ScannerDirection | None, Query()] = None,
    min_grade: Annotated[ScannerGrade | None, Query()] = None,
    include_closed: Annotated[bool, Query()] = False,
    sort_by: Annotated[TradeSortBy, Query()] = TradeSortBy.OPENED_AT_DESC,
) -> ManagedTradeRecordList:
    """Return open trades only after current Binance Demo position verification."""

    normalized_symbol = symbol.strip().upper() if symbol is not None else None
    if normalized_symbol is not None and (not normalized_symbol or not normalized_symbol.isalnum()):
        raise HTTPException(status_code=422, detail="Invalid symbol")
    return service.trades(
        TradeListFilters(
            symbol=normalized_symbol,
            direction=direction,
            min_grade=min_grade,
            include_closed=include_closed,
            sort_by=sort_by,
        )
    )


@router.post("/close/{trade_id}", response_model=DemoTradeRecord)
async def close_trade(
    trade_id: Annotated[str, Path(min_length=36, max_length=36)],
    request: TradeCloseRequest,
    service: DurableTradeManagementService = Depends(  # noqa: B008
        get_durable_trade_management_service
    ),
    _authorization: MutationAuthorization = Depends(authorize_mutation),  # noqa: B008
) -> DemoTradeRecord:
    """Close one Demo trade through durable, retry-safe orchestration."""

    return service.close_trade(trade_id, request)
