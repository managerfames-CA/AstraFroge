from datetime import timedelta

from app.scanner.constants import (
    ACTIVE_REFRESH_INTERVAL,
    FULL_SCAN_INTERVAL,
    UNIVERSE_REFRESH_INTERVAL,
)
from app.schemas.scanner import ScannerCandidateSummary, ScannerRunSummary


def test_scanner_scheduler_cadence_is_locked() -> None:
    assert UNIVERSE_REFRESH_INTERVAL == timedelta(minutes=30)
    assert FULL_SCAN_INTERVAL == timedelta(minutes=15)
    assert ACTIVE_REFRESH_INTERVAL == timedelta(minutes=5)


def test_scanner_summary_contract_exposes_prefilter_pipeline_counts() -> None:
    run_fields = ScannerRunSummary.model_fields
    candidate_summary_fields = ScannerCandidateSummary.model_fields

    expected = {
        "prefilter_pool_symbols",
        "directional_symbols",
        "prefilter_filtered_symbols",
        "evaluated_symbols",
    }

    assert expected <= set(run_fields)
    assert expected <= set(candidate_summary_fields)
