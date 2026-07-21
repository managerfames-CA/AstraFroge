# BE-06 Completion Evidence

## Locked requirement

Recover open orders and positions after restart or deployment.

## Completion candidate

- Date/time: **2026-07-19 20:02:51 BDT**
- Branch: `be-06-restart-deployment-recovery`
- Final PR, merge commit and CI evidence: pending owner-approved merge path

## Audited existing recovery foundation

- `PersistentExecutionService._recover_trades()` reconstructs durable `DemoTradeRecord` objects when a new process-scoped execution service is created.
- `StartupRecoveryCoordinator` runs before the execution worker, compares durable open trades with Binance Demo positions and protective orders, verifies entry/protective identities, then unlocks automation only after exchange and Signal revalidation.
- `tests/unit/test_persistence.py::test_application_restart_recovers_runtime_trade_service` already proves a new application process can reload a stored open trade from the same database.
- Existing recovery-gate tests prove new entries remain blocked before startup reconciliation completes.

## BE-06 implementation added

- Added typed `RestartRecoveryReport` and `RestartRecoveryState` contracts.
- Added `RestartRecoveryOwnershipService` to produce a read-only, secret-safe proof that rehydrated durable open trades own the exact current Binance Demo protective orders and non-zero positions.
- Added recovered trade IDs, protective client-order IDs, position symbols and exact counts.
- Added explicit blocking classifications for missing configuration, unavailable exchange truth, invalid/duplicate payloads, unsafe protective-order status, duplicate durable ownership and order/position set mismatch.
- Added `GET /api/v1/restart-recovery/status`.
- No exchange mutation, order placement, cancellation or position change is performed by the BE-06 report service.

## Focused verification

- Matching rehydrated trade + two protective orders + one position reports `RECOVERED`.
- Verified empty durable/exchange state reports `RECOVERED` with zero counts.
- Startup exchange reconciliation not complete reports `NOT_READY`.
- Missing Demo client or exchange failure reports `BLOCKED`.
- Duplicate durable trades/orders fail closed.
- Invalid, duplicate, partially filled or mismatched exchange orders fail closed.
- Invalid, duplicate or mismatched exchange positions fail closed.
- API and OpenAPI contracts include the restart-recovery status surface.

Focused tests: `tests/unit/test_be_06_restart_recovery.py` plus the existing durable restart regression in `tests/unit/test_persistence.py`.

## Excluded

- BE-07 global fail-closed orchestration across every reconciliation/report surface.
- Journal construction, PnL, commissions and funding.
- Active Trades exchange-authoritative frontend contract.
- Live or real-money execution.
