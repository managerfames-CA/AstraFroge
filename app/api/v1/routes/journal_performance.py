"""Journal and Performance Engine API routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.v1.dependencies import get_journal_performance_service
from app.schemas.execution import DemoTradeCloseReason
from app.schemas.journal_performance import (
    JournalEntryList,
    JournalFilters,
    JournalPerformanceStatusResponse,
    JournalSortBy,
    PerformanceSnapshotResponse,
)
from app.schemas.performance_reporting import VerifiedPerformanceReportResponse
from app.schemas.scanner import ScannerDirection, ScannerGrade
from app.services.journal_performance import JournalPerformanceService
from app.services.performance_reporting import VerifiedPerformanceReportingService

router = APIRouter(prefix="/journal-performance", tags=["journal-performance"])


@router.get("/status", response_model=JournalPerformanceStatusResponse)
async def journal_performance_status(
    service: JournalPerformanceService = Depends(get_journal_performance_service),  # noqa: B008
) -> JournalPerformanceStatusResponse:
    """Return current journal-performance readiness and 30-day headline metrics."""

    return service.status()


@router.get("/journal", response_model=JournalEntryList)
async def closed_trade_journal(
    service: JournalPerformanceService = Depends(get_journal_performance_service),  # noqa: B008
    symbol: Annotated[str | None, Query()] = None,
    direction: Annotated[ScannerDirection | None, Query()] = None,
    min_grade: Annotated[ScannerGrade | None, Query()] = None,
    close_reason: Annotated[DemoTradeCloseReason | None, Query()] = None,
    sort_by: Annotated[JournalSortBy, Query()] = JournalSortBy.CLOSED_AT_DESC,
) -> JournalEntryList:
    """Return filtered closed-trade journal entries."""

    normalized_symbol = symbol.strip().upper() if symbol is not None else None
    if normalized_symbol is not None and (
        not normalized_symbol or not normalized_symbol.isalnum()
    ):
        raise HTTPException(status_code=422, detail="Invalid symbol")
    return service.journal(
        JournalFilters(
            symbol=normalized_symbol,
            direction=direction,
            min_grade=min_grade,
            close_reason=close_reason,
            sort_by=sort_by,
        )
    )


@router.get("/performance", response_model=PerformanceSnapshotResponse)
async def performance_snapshot(
    service: JournalPerformanceService = Depends(get_journal_performance_service),  # noqa: B008
    lookback_days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> PerformanceSnapshotResponse:
    """Return windowed performance metrics from closed tracked trades."""

    return service.performance(lookback_days=lookback_days)


@router.get("/reports", response_model=VerifiedPerformanceReportResponse)
async def verified_performance_reports(
    service: JournalPerformanceService = Depends(get_journal_performance_service),  # noqa: B008
    lookback_days: Annotated[int, Query(ge=1, le=3650)] = 30,
) -> VerifiedPerformanceReportResponse:
    """Return strategy, symbol, daily, weekly and monthly verified-trade reports."""

    reporting = VerifiedPerformanceReportingService(service)
    return reporting.report(lookback_days=lookback_days)
