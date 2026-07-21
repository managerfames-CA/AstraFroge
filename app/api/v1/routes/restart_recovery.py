"""BE-06 restart/deployment recovery ownership observability."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.schemas.restart_recovery import RestartRecoveryReport
from app.services.restart_recovery import RestartRecoveryOwnershipService

router = APIRouter(
    prefix="/restart-recovery",
    tags=["restart-recovery"],
)


@router.get("/status", response_model=RestartRecoveryReport)
def restart_recovery_status(request: Request) -> RestartRecoveryReport:
    """Expose read-only proof of recovered Demo order/position ownership."""

    service = getattr(request.app.state, "restart_recovery_service", None)
    if not isinstance(service, RestartRecoveryOwnershipService):
        raise HTTPException(status_code=503, detail="Restart recovery service is unavailable")
    return service.report()
