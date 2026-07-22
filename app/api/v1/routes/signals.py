"""Signal Engine API routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.api.v1.dependencies import get_signal_service
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.signals import (
    SignalLifecycle,
    SignalRecord,
    SignalRecordList,
    SignalStatusResponse,
)
from app.services.signals import SignalService

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/status", response_model=SignalStatusResponse)
async def signal_status(
    service: SignalService = Depends(get_signal_service),  # noqa: B008
) -> SignalStatusResponse:
    """Return the current deterministic Signal Engine state."""

    return service.status()


@router.get("", response_model=SignalRecordList)
async def signal_list(
    service: SignalService = Depends(get_signal_service),  # noqa: B008
    symbol: Annotated[str | None, Query()] = None,
    direction: Annotated[ScannerDirection | None, Query()] = None,
    setup: Annotated[ScannerSetup | None, Query()] = None,
    grade: Annotated[ScannerGrade | None, Query()] = None,
    lifecycle: Annotated[SignalLifecycle | None, Query()] = None,
) -> SignalRecordList:
    """Return filtered versioned Signals sourced from Scanner candidates."""

    normalized_symbol = symbol.strip().upper() if symbol is not None else None
    if normalized_symbol is not None and (
        not normalized_symbol or not normalized_symbol.isalnum()
    ):
        raise HTTPException(status_code=422, detail="Invalid symbol")
    signals = [
        signal
        for signal in service.signals().signals
        if (normalized_symbol is None or signal.symbol == normalized_symbol)
        and (direction is None or signal.direction is direction)
        and (setup is None or signal.setup is setup)
        and (grade is None or signal.grade is grade)
        and (lifecycle is None or signal.lifecycle is lifecycle)
    ]
    return SignalRecordList(count=len(signals), signals=signals)


@router.get("/{signal_id}", response_model=SignalRecord)
async def signal_detail(
    signal_id: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")],
    service: SignalService = Depends(get_signal_service),  # noqa: B008
) -> SignalRecord:
    """Return one stable Signal record and its lifecycle audit history."""

    signal = service.get(signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    return signal
