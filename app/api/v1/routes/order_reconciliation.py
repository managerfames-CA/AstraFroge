"""Continuous Binance Demo order reconciliation observability."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.schemas.order_reconciliation import OrderReconciliationReport
from app.services.order_reconciliation import ContinuousOrderReconciliationService

router = APIRouter(
    prefix="/order-reconciliation",
    tags=["order-reconciliation"],
)


@router.get("/status", response_model=OrderReconciliationReport)
async def order_reconciliation_status(request: Request) -> OrderReconciliationReport:
    service = getattr(request.app.state, "order_reconciliation_service", None)
    if not isinstance(service, ContinuousOrderReconciliationService):
        raise HTTPException(status_code=503, detail="Order reconciliation service is unavailable")
    return service.latest()
