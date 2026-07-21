# BE-07 Completion Evidence

## Locked requirement

Fail closed whenever reconciliation cannot prove a safe exchange state.

## Completion candidate

- Date/time: **2026-07-19 20:24 BDT**
- Branch: `be-07-global-reconciliation-fail-closed`
- PR, merge commit and final CI evidence: pending owner-approved merge path

## Implementation

- Added `GlobalReconciliationSafetyService` as the single combined authority for continuous order reconciliation, continuous position reconciliation, lifecycle mismatch classification and restart/deployment ownership recovery.
- Added a mandatory global reconciliation cycle after startup recovery and before the execution worker starts.
- The worker, execution-leader monitor and continuous global monitor start only when the combined report is `SAFE` and the recovery gate remains ready.
- Any order drift, position drift, lifecycle mismatch, restart-recovery mismatch, unavailable source or unexpected evaluation failure calls `AutomationRecoveryGate.fail(...)` and keeps automated execution locked.
- Order and position route lifespans no longer launch independent reconciliation loops; the app-scoped global authority owns the continuous cycle.
- Added `GET /api/v1/global-reconciliation/status` with a secret-safe typed report.
- No exchange mutation, strategy change, Journal construction or live-trading path was added.

## Focused verification

- All four safe surfaces produce `SAFE` and preserve automation readiness.
- Order drift produces `BLOCKED`, records the component errors and fails the global gate.
- Position unavailability produces `BLOCKED` and fails the global gate.
- Restart/deployment ownership mismatch produces `BLOCKED` and fails the global gate.
- Unexpected source failure produces `UNAVAILABLE` and fails the global gate.
- The continuous monitor stops immediately after a blocking cycle.
- Execution-disabled API startup exposes `NOT_RUN`, `blocking=true` and `automation_ready=false`.
- OpenAPI includes the global reconciliation status endpoint.

Focused tests: `tests/unit/test_be_07_global_reconciliation.py` and `tests/contract/test_health_contract.py`.

## Excluded

- BE-08 verified Journal record construction.
- BE-09 realized PnL calculation.
- BE-10 commissions and funding attribution.
- Live or real-money execution.
