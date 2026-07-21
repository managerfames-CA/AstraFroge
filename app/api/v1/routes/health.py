"""Liveness and readiness endpoints."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.schemas.common import RuntimeState
from app.schemas.health import LiveResponse, ReadyResponse

router = APIRouter(prefix="/health", tags=["health"])


def _demo_account_state(settings: Settings) -> RuntimeState:
    if settings.execution_enabled and settings.demo_credentials_configured:
        return RuntimeState.DEGRADED
    if settings.demo_credentials_configured:
        return RuntimeState.BLOCKED
    return RuntimeState.NOT_CONFIGURED


@router.get("", response_model=ReadyResponse)
def health(settings: Annotated[Settings, Depends(get_settings)]) -> ReadyResponse:
    """Report readiness at /health for deployed frontend compatibility."""

    return ready(settings)


@router.get("/live", response_model=LiveResponse)
def live(settings: Annotated[Settings, Depends(get_settings)]) -> LiveResponse:
    """Confirm that the API process is running."""

    return LiveResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
        timestamp=datetime.now(UTC),
    )


@router.get("/ready", response_model=ReadyResponse)
def ready(settings: Annotated[Settings, Depends(get_settings)]) -> ReadyResponse:
    """Report foundation readiness without claiming downstream connectivity."""

    return ReadyResponse(
        status="ready",
        service=settings.app_name,
        version=settings.app_version,
        execution_status=(
            RuntimeState.DEGRADED if settings.execution_enabled else RuntimeState.BLOCKED
        ),
        market_data_status=RuntimeState.NOT_CONFIGURED,
        demo_account_status=_demo_account_state(settings),
        timestamp=datetime.now(UTC),
    )
