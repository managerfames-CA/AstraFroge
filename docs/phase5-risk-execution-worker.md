# Phase 5 — Risk-to-Execution Boundary and Single Demo Execution Worker

## Architecture

```text
Phase 4 READY Signal Decision
→ current Risk approval
→ fresh AccountSnapshot
→ durable ExecutionCommand
→ atomic single-worker claim
→ revalidation
→ worker-owned Binance Demo execution backend
→ verified filled entry
→ verified Stop Loss and Take Profit
→ protected open trade
→ completed command
```

Scanner, StrategyEvaluationService, SignalDecisionEngine, SignalService, RiskService, API routes, and generic background loops do not submit new Binance Demo entries.

The existing Demo execution orchestration remains the verified query-before-retry order backend, but only `DemoExecutionWorker` receives that backend. `WorkerIsolatedExecutionService` preserves read and trade-management compatibility while rejecting direct new-entry activation. The legacy `auto_execute_pending()` compatibility call queues durable commands only.

## Execution command contract

`ExecutionCommand` is immutable and records:

- deterministic command, idempotency, and execution identities
- Signal ID and Phase 4 decision key
- Risk decision identity
- symbol, direction, setup, and A+/A grade
- Phase 2 source snapshot version
- approved entry, Stop Loss, Take Profit, R multiple, quantity, notional, margin, and leverage
- account snapshot ID
- deterministic entry/SL/TP client order IDs
- state, timestamps, expiry, claim ownership, exchange identities, fill values, failure reason, and audit codes

Commands are created only from a current READY/ACTIVE A+/A signal with a ready trigger, no watch/rejection reasons, verified provenance, current APPROVED Risk assessment, and a fresh account snapshot.

## State machine

The durable states are:

- `PENDING`
- `CLAIMED`
- `SUBMITTING`
- `ENTRY_CONFIRMED`
- `PROTECTION_PENDING`
- `PROTECTED`
- `COMPLETED`
- `BLOCKED`
- `FAILED`
- `RECOVERY_REQUIRED`
- `EXPIRED`

Transitions are centrally validated. Illegal transitions raise `IllegalExecutionCommandTransition`. A command is never completed from an HTTP submission response alone; completion follows verified fill, verified Stop Loss and Take Profit identities, and durable trade storage.

## Idempotency and single-worker ownership

Command identity is SHA-256 over the immutable operation, Signal ID, Phase 4 decision key, and source snapshot version. Database uniqueness protects both the idempotency key and the Signal/decision/snapshot tuple.

PostgreSQL claims use `SELECT ... FOR UPDATE SKIP LOCKED`. A bounded stale claim becomes `RECOVERY_REQUIRED` before re-claim. Stable client IDs are derived from the Signal ID. Entry and protective operations query deterministic exchange identity before submission and query again after ambiguous transport failure.

## Risk revalidation

Immediately before submission the worker re-proves:

- Phase 4 READY status and active lifecycle
- A+/A grade and ready trigger
- decision/source snapshot identity and expiry
- fresh account snapshot and account ability to trade
- current Risk approval, quantity, notional, margin, leverage, Stop Loss, target, and direction
- absence of existing exchange position conflict
- startup recovery readiness
- valid PostgreSQL execution-leader lease

Any mismatch blocks the command before an exchange mutation.

## Crash and ambiguous-result behavior

- A lost entry response is recovered by deterministic query-before-retry in the existing execution backend.
- Protective orders query deterministic Algo identity before submit and after an ambiguous failure.
- Partial/unverified fills, invalid identities, one-sided protection, protection failure, database failure after mutation, or unexpected worker failure become `RECOVERY_REQUIRED` or fail the Phase 1 recovery gate closed.
- Restart recovery never creates a second command for the same immutable execution identity.

## Execution-disabled behavior

`execution_enabled=false` remains the default. In disabled mode:

- no worker loop is started
- `process_one()` performs zero private write calls
- durable commands remain observable without mutating Binance
- API status and read-only command endpoints remain healthy
- Real Trading is not available

## API compatibility

Existing execution status, account, plan, and trade reads remain available. `POST /api/v1/execution/demo/activate/{signal_id}` now enqueues a durable command and never submits an order directly. Client-supplied quantity remains rejected.

Read-only observability endpoints:

- `GET /api/v1/execution/demo/commands/status`
- `GET /api/v1/execution/demo/commands`
- `GET /api/v1/execution/demo/commands/{command_id}`
- `GET /api/v1/execution/demo/commands/{command_id}/history`
- `GET /api/v1/execution/demo/worker/status`

## Migration list

1. `20260717_0001_durable_trading_state.py`
2. existing revision `20260717_0002`
3. `20260719_0002_execution_commands.py`

The Phase 5 migration is additive and creates `execution_commands` and `execution_command_transitions` with deterministic unique constraints and state/expiry/history indexes. Previous migrations are not rewritten.

## Preserved boundaries

- Phase 1 recovery and PostgreSQL execution-leader safety
- Phase 2 shared market/account snapshot provenance
- Phase 3 Scanner/Strategy separation
- Phase 4 READY / NEAR_SETUP / REJECTED ownership
- B+ and NEAR_SETUP remain non-executable
- strategy formulas, indicator calculations, scanner thresholds, grade mappings, and locked Risk rules are unchanged
- existing trade-management close behavior is outside the new-entry worker boundary

## Known limitations

- Automated tests use fakes and the repository SQLite migration harness; no Binance Demo order is submitted.
- PostgreSQL `SKIP LOCKED` behavior is implemented but not exercised against a live multi-process PostgreSQL cluster in CI.
- The worker processes one command per cycle; horizontal throughput tuning is intentionally deferred.
- Phase 6 reconciliation and trade-management redesign are not included. PR #35 is untouched and must be rebased, retested, and re-audited only after Phase 5 is approved and merged.
