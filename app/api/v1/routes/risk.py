"""Risk Engine API routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.v1.dependencies import get_risk_service
from app.schemas.risk import RiskAssessmentList, RiskDecision, RiskStatusResponse
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.signals import SignalLifecycle
from app.services.risk import RiskService

router = APIRouter(prefix="/risk", tags=["risk"])


@router.get("/status", response_model=RiskStatusResponse)
async def risk_status(
    service: RiskService = Depends(get_risk_service),  # noqa: B008
) -> RiskStatusResponse:
    """Return current deterministic Risk Engine state."""

    return service.status()


@router.get("/assessments", response_model=RiskAssessmentList)
async def risk_assessments(
    service: RiskService = Depends(get_risk_service),  # noqa: B008
    symbol: Annotated[str | None, Query()] = None,
    direction: Annotated[ScannerDirection | None, Query()] = None,
    setup: Annotated[ScannerSetup | None, Query()] = None,
    grade: Annotated[ScannerGrade | None, Query()] = None,
    lifecycle: Annotated[SignalLifecycle | None, Query()] = None,
    decision: Annotated[RiskDecision | None, Query()] = None,
) -> RiskAssessmentList:
    """Return filtered deterministic risk assessments."""

    normalized_symbol = symbol.strip().upper() if symbol is not None else None
    if normalized_symbol is not None and (not normalized_symbol or not normalized_symbol.isalnum()):
        raise HTTPException(status_code=422, detail="Invalid symbol")
    assessments = [
        assessment
        for assessment in service.assessments().assessments
        if (normalized_symbol is None or assessment.symbol == normalized_symbol)
        and (direction is None or assessment.direction is direction)
        and (setup is None or assessment.setup is setup)
        and (grade is None or assessment.grade is grade)
        and (lifecycle is None or assessment.signal_lifecycle is lifecycle)
        and (decision is None or assessment.decision is decision)
    ]
    return RiskAssessmentList(count=len(assessments), assessments=assessments)
