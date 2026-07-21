"""Global reconciliation fail-closed observability."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.schemas.global_reconciliation import GlobalReconciliationReport
from app.services.global_reconciliation import GlobalReconciliationSafetyService

router = APIRouter(
    prefix="/global-reconciliation",
    tags=["global-reconciliation"],
)


@router.get("/status", response_model=GlobalReconciliationReport)
def global_reconciliation_status(request: Request) -> GlobalReconciliationReport:
    service = getattr(request.app.state, "global_reconciliation_service", None)
    if not isinstance(service, GlobalReconciliationSafetyService):
        raise HTTPException(
            status_code=503,
            detail="Global reconciliation service is unavailable",
        )
    return service.latest()
