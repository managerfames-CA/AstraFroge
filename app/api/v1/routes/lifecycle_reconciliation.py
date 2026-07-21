"""BE-05 combined lifecycle mismatch observability."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.schemas.lifecycle_reconciliation import LifecycleReconciliationReport
from app.services.lifecycle_reconciliation import LifecycleMismatchDetectionService
from app.services.order_reconciliation import ContinuousOrderReconciliationService
from app.services.position_reconciliation import (
    ContinuousPositionReconciliationService,
)

router = APIRouter(
    prefix="/lifecycle-reconciliation",
    tags=["lifecycle-reconciliation"],
)


@router.get("/status", response_model=LifecycleReconciliationReport)
async def lifecycle_reconciliation_status(
    request: Request,
) -> LifecycleReconciliationReport:
    order_service = getattr(request.app.state, "order_reconciliation_service", None)
    position_service = getattr(
        request.app.state,
        "position_reconciliation_service",
        None,
    )
    if not isinstance(order_service, ContinuousOrderReconciliationService):
        raise HTTPException(
            status_code=503,
            detail="Order reconciliation service is unavailable",
        )
    if not isinstance(position_service, ContinuousPositionReconciliationService):
        raise HTTPException(
            status_code=503,
            detail="Position reconciliation service is unavailable",
        )
    return LifecycleMismatchDetectionService(
        order_service,
        position_service,
    ).latest()
