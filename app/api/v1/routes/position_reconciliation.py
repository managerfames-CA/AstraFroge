"""Continuous Binance Demo position reconciliation observability."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.schemas.position_reconciliation import PositionReconciliationReport
from app.services.position_reconciliation import (
    ContinuousPositionReconciliationService,
)

router = APIRouter(
    prefix="/position-reconciliation",
    tags=["position-reconciliation"],
)


@router.get("/status", response_model=PositionReconciliationReport)
async def position_reconciliation_status(
    request: Request,
) -> PositionReconciliationReport:
    service = getattr(request.app.state, "position_reconciliation_service", None)
    if not isinstance(service, ContinuousPositionReconciliationService):
        raise HTTPException(
            status_code=503,
            detail="Position reconciliation service is unavailable",
        )
    return service.latest()
