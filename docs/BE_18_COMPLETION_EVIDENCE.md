# BE-18 COMPLETION EVIDENCE: SCANNER LATEST-RUN AND DEGRADED CONTRACT

## 1. Executive Summary

AstraForge Crypto has successfully updated and aligned the Scanner latest-run summaries and degraded-run contract with the frontend specifications. All response models are strongly-typed, completely backwards-compatible, and truthful under technical partial failures.

## 2. Technical Design and Implementation

### 2.1 Latest-Run States
* Integrated the stable enum `ScannerRunStatus` with the required states:
  * `RUNNING`
  * `COMPLETED` (success)
  * `DEGRADED`
  * `FAILED`
  * `SKIPPED`
* Enriched `ScannerRunSummary` with calculated, strongly-typed fields:
  * `audit_count: int`
  * `degraded_state: bool`
  * `diagnostic_codes: list[str]`
  * `affected_symbol_count: int`
  * `results_usable: bool`
  * `execution_eligibility_blocked: bool`
* Values are computed dynamically inside Pydantic model validators on instantiation. No data is fabricated.

### 2.2 Deterministic Degraded Behavior
* When symbol-level technical failures occur but some symbols evaluate successfully:
  * Status is set to `DEGRADED`.
  * Usable results remain `results_usable = True`.
  * `affected_symbol_count` is set truthfully to the number of failed symbols.
* Total dependency failure (0 successful symbols) is correctly marked as `FAILED` (`results_usable = False`, `execution_eligibility_blocked = True`).
* Normal strategy rejections, score below 80, confidence below 60, and no-setup results are classified as normal `COMPLETED` runs and do *not* trigger degraded status.

### 2.3 Typed Diagnostic Model
* `ScannerAuditRecord` has been extended with the required BE-18 properties:
  * `severity: str | None` (e.g. "error", "warning", "info")
  * `reference_timestamp: datetime | None` (synchronized with `reference_time`)
  * `retryable: bool | None`
  * `blocking: bool | None`
* These fields are inferred dynamically based on the diagnostic/audit code. Raw exception traces, credentials, and signed requests are never serialized, ensuring complete safety.

## 3. Verification Evidence

### 3.1 Tests Executed
Our dedicated test suite explicitly covers all BE-18 requirements:
18. `test_be18_18_no_latest_run_is_represented_truthfully`
19. `test_be18_19_running_latest_run_response`
20. `test_be18_20_successful_latest_run_response`
21. `test_be18_21_degraded_latest_run_response`
22. `test_be18_22_failed_latest_run_response`
23. `test_be18_23_skipped_duplicate_run_response`
24. `test_be18_24_symbol_level_technical_failures_create_degraded_status`
25. `test_be18_25_total_dependency_failure_remains_failed`
26. `test_be18_26_normal_rejections_do_not_create_false_degraded_status`
27. `test_be18_27_diagnostic_models_remain_typed_and_secret_safe`
28. `test_be18_28_existing_scanner_route_contracts_continue_to_pass`
29. `test_be18_29_openapi_publishes_required_scanner_schemas`

All 12 BE-18 tests passed cleanly.

### 3.2 OpenAPI Schema Validation
* Verified that `/api/v1/openapi.json` correctly publishes schemas for `ScannerStatusResponse`, `ScannerRunSummary`, and `ScannerAuditRecord` with all newly added properties.

## 4. Known Limitations
* The `ScannerRunSummary` diagnostics are stored process-local; multi-process systems only see the run history of the instance that served the request unless persistent backend databases are extended to save full run summaries.
