"""Universe Engine API route."""

from fastapi import APIRouter, Depends, HTTPException

from app.api.v1.dependencies import get_universe_service
from app.integrations.binance.public_client import BinancePublicClientError
from app.schemas.universe import UniverseSnapshot
from app.services.universe import UniverseService

router = APIRouter(prefix="/universe", tags=["universe"])


@router.get("", response_model=UniverseSnapshot)
async def universe_snapshot(
    service: UniverseService = Depends(get_universe_service),  # noqa: B008
) -> UniverseSnapshot:
    """Return the current ranked universe and complete rejection audit."""

    try:
        return await service.build()
    except BinancePublicClientError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
