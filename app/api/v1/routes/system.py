"""System status endpoint."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.v1.routes.health import _demo_account_state
from app.core.config import Settings, get_settings
from app.schemas.common import RuntimeState
from app.schemas.health import SystemStatusResponse

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status", response_model=SystemStatusResponse)
def system_status(
    settings: Annotated[Settings, Depends(get_settings)],
) -> SystemStatusResponse:
    """Return factual foundation state only."""

    return SystemStatusResponse(
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        execution_enabled=settings.execution_enabled,
        market_data_status=RuntimeState.NOT_CONFIGURED,
        demo_account_status=_demo_account_state(settings),
        timestamp=datetime.now(UTC),
    )
