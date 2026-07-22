"""Scanner Engine Runtime API routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.v1.dependencies import get_scanner_service
from app.core.security import MutationAuthorization, authorize_mutation
from app.schemas.scanner import (
    CandidateLifecycle,
    ScannerCandidateList,
    ScannerCandidateSummary,
    ScannerDirection,
    ScannerRunSummary,
    ScannerRunType,
    ScannerSetup,
    ScannerStatusResponse,
)
from app.services.scanner import ScannerService

router = APIRouter(prefix="/scanner", tags=["scanner"])

_PREFILTER_REJECTION_CODES = {
    "trend_sideways",
    "trend_mixed",
    "trend_not_directional",
    "trend_prefilter_failed",
}


def _latest_full_scan(service: ScannerService) -> ScannerRunSummary | None:
    """Return the latest full-universe run retained by the process."""

    runs = getattr(service, "_runs", None)
    if not isinstance(runs, list):
        return None
    for run in reversed(runs):
        if isinstance(run, ScannerRunSummary) and run.run_type == ScannerRunType.FULL_UNIVERSE_SCAN:
            return run
    return None


def _enrich_pipeline_counts(
    service: ScannerService, run: ScannerRunSummary | None
) -> ScannerRunSummary | None:
    """Attach exact broad-pool and 1H prefilter counts to a full scan summary."""

    if run is None or run.run_type != ScannerRunType.FULL_UNIVERSE_SCAN:
        return run
    universe_service = getattr(service, "_universe", None)
    snapshot = getattr(universe_service, "_cached_snapshot", None)
    candidates = getattr(snapshot, "candidates", None)
    rejections = getattr(snapshot, "rejections", None)
    if not isinstance(candidates, list) or not isinstance(rejections, list):
        return run

    prefilter_filtered = sum(
        1
        for rejection in rejections
        if getattr(rejection, "code", None) in _PREFILTER_REJECTION_CODES
    )
    directional_overflow = sum(
        1
        for rejection in rejections
        if getattr(rejection, "code", None) == "directional_universe_limit"
    )
    directional = len(candidates) + directional_overflow
    run.prefilter_pool_symbols = directional + prefilter_filtered
    run.directional_symbols = directional
    run.prefilter_filtered_symbols = prefilter_filtered
    return run


def _candidate_summary(service: ScannerService) -> ScannerCandidateSummary:
    latest = service.latest_run()
    status = service.status()
    if latest is None:
        return ScannerCandidateSummary(state=status.state)

    summary_source = latest
    if (
        latest.run_type == ScannerRunType.ACTIVE_CANDIDATE_REFRESH
        and latest.evaluated_symbols == 0
        and status.active_candidate_count == 0
    ):
        summary_source = _latest_full_scan(service) or latest
    _enrich_pipeline_counts(service, summary_source)

    return ScannerCandidateSummary(
        state=status.state,
        run_status=summary_source.status,
        run_type=summary_source.run_type,
        run_started_at=summary_source.run_started_at,
        completed_at=summary_source.completed_at,
        prefilter_pool_symbols=summary_source.prefilter_pool_symbols,
        directional_symbols=summary_source.directional_symbols,
        prefilter_filtered_symbols=summary_source.prefilter_filtered_symbols,
        evaluated_symbols=summary_source.evaluated_symbols,
        successful_symbols=summary_source.successful_symbols,
        failed_symbols=summary_source.failed_symbols,
        discovered_candidates=summary_source.discovered_candidates,
        selected_candidates=summary_source.selected_candidates,
        updated_candidates=summary_source.updated_candidates,
        qualified_candidates=summary_source.qualified_candidates,
        audits=summary_source.audits,
    )


@router.get("/status", response_model=ScannerStatusResponse)
async def scanner_status(
    service: ScannerService = Depends(get_scanner_service),  # noqa: B008
) -> ScannerStatusResponse:
    """Return honest process-scoped Scanner state."""

    status = service.status()
    _enrich_pipeline_counts(service, status.latest_run)
    return status


@router.post("/start", response_model=ScannerStatusResponse)
async def scanner_start(
    service: ScannerService = Depends(get_scanner_service),  # noqa: B008
    _authorization: MutationAuthorization = Depends(authorize_mutation),  # noqa: B008
) -> ScannerStatusResponse:
    """Enable Scanner only after operator authorization and replay protection."""

    status = await service.start()
    _enrich_pipeline_counts(service, status.latest_run)
    return status


@router.post("/stop", response_model=ScannerStatusResponse)
async def scanner_stop(
    service: ScannerService = Depends(get_scanner_service),  # noqa: B008
    _authorization: MutationAuthorization = Depends(authorize_mutation),  # noqa: B008
) -> ScannerStatusResponse:
    """Disable recurring Scanner work after an authorized mutation request."""

    status = await service.stop()
    _enrich_pipeline_counts(service, status.latest_run)
    return status


@router.post("/run-now", response_model=ScannerRunSummary)
async def scanner_run_now(
    service: ScannerService = Depends(get_scanner_service),  # noqa: B008
    _authorization: MutationAuthorization = Depends(authorize_mutation),  # noqa: B008
) -> ScannerRunSummary:
    """Run one authorized Full Universe Scan without changing ON/OFF state."""

    run = await service.run_now()
    return _enrich_pipeline_counts(service, run) or run


@router.get("/candidates", response_model=ScannerCandidateList)
async def scanner_candidates(
    service: ScannerService = Depends(get_scanner_service),  # noqa: B008
    symbol: Annotated[str | None, Query()] = None,
    direction: Annotated[ScannerDirection | None, Query()] = None,
    setup: Annotated[ScannerSetup | None, Query()] = None,
    lifecycle: Annotated[CandidateLifecycle | None, Query()] = None,
) -> ScannerCandidateList:
    """Return filtered deterministic Scanner candidates."""

    normalized_symbol = symbol.strip().upper() if symbol is not None else None
    if normalized_symbol is not None and (not normalized_symbol or not normalized_symbol.isalnum()):
        raise HTTPException(status_code=422, detail="Invalid symbol")
    candidates = [
        candidate
        for candidate in service.candidates()
        if (normalized_symbol is None or candidate.symbol == normalized_symbol)
        and (direction is None or candidate.direction is direction)
        and (setup is None or candidate.setup is setup)
        and (lifecycle is None or candidate.lifecycle is lifecycle)
    ]
    return ScannerCandidateList(
        count=len(candidates),
        candidates=candidates,
        summary=_candidate_summary(service),
    )


@router.get("/runs/latest", response_model=ScannerRunSummary)
async def scanner_latest_run(
    service: ScannerService = Depends(get_scanner_service),  # noqa: B008
) -> ScannerRunSummary:
    """Return the latest Scanner run summary."""

    latest = service.latest_run()
    if latest is None:
        raise HTTPException(status_code=404, detail="No Scanner run is available")
    return _enrich_pipeline_counts(service, latest) or latest
