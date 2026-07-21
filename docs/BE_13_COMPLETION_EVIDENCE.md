# BE-13 Completion Evidence

## Locked requirement

**BE-13:** Verify partial close, Stop Loss and Take Profit lifecycle events.

## Implementation

- `app/services/protective_lifecycle.py`
  - Reads current Binance Demo positions, both protective Algo orders and bounded user-trade fills.
  - Attributes Stop Loss or Take Profit only through exact client-order, Algo-order and actual-order identities.
  - Requires protective executed quantity, verified fills and remaining exchange position to agree exactly.
  - Rejects conflicting Stop Loss and Take Profit fills, malformed evidence, truncated history and unexplained position changes.
  - Persists one deterministic lifecycle event per exchange fill under a locked durable trade row.
  - Applies the same event sequence idempotently across retry, restart and multiple processes.
  - Reuses verified Journal fill and income authorities for full-close PnL, commission and funding economics.
  - Cancels the unused sibling protective order after a verified full close and records cleanup state.

- `app/schemas/execution.py`
  - Preserves original entry quantity while recording verified remaining quantity and protective-exit evidence.

- `app/services/global_reconciliation.py` and `app/main.py`
  - Run protective lifecycle verification before order and position safety proofs.
  - Fail the global automation gate when lifecycle evidence is ambiguous or incomplete.

- `GET /api/v1/protective-lifecycle/status`
  - Publishes the latest typed lifecycle verification report without exposing secrets.

## Partial-close safety boundary

A verified partial close is persisted, but automation remains fail closed. BE-13 does not resize or replace protective orders for the remaining position. Existing Active Trades and position reconciliation therefore continue to reject quantity drift until protection is deliberately restored in a later authorized scope.

## Verification

- No-fill plus unchanged position remains in sync.
- Partial Stop Loss fill is durably recorded once and remains blocking.
- Full Take Profit closes only from verified order, fill, position and income evidence.
- Conflicting Stop Loss and Take Profit fills cause no lifecycle mutation.
- Position reduction without matching fills fails closed.
- API and OpenAPI contracts include protective lifecycle status.

## Scope boundary

BE-14 order-field reporting is not included.
