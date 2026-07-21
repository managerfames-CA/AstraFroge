"""Protective lifecycle exchange-truth observability."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.schemas.protective_lifecycle import ProtectiveLifecycleReport
from app.services.protective_lifecycle import ProtectiveLifecycleVerificationService

router = APIRouter(
    prefix="/protective-lifecycle",
    tags=["protective-lifecycle"],
)


@router.get("/status", response_model=ProtectiveLifecycleReport)
def protective_lifecycle_status(request: Request) -> ProtectiveLifecycleReport:
    service = getattr(request.app.state, "protective_lifecycle_service", None)
    if not isinstance(service, ProtectiveLifecycleVerificationService):
        raise HTTPException(
            status_code=503,
            detail="Protective lifecycle service is unavailable",
        )
    return service.latest()
