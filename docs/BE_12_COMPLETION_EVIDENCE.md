# BE-12 Completion Evidence

## Locked requirement

**BE-12:** Make manual close operations durable and idempotent.

## Implementation

- `app/services/manual_close_durability.py`
  - Persists a deterministic `CLOSE:<trade_id>` intent before exchange mutation.
  - Keeps the close client-order ID immutable.
  - Persists `PENDING`, `SUBMITTED`, `FILLED`, `RECOVERY_REQUIRED` and `COMPLETED` states.
  - Persists the exchange order ID, status, executed quantity and average fill price.
  - Uses a separate deterministic SHA-256 close-order row identity that fits the database `VARCHAR(64)` limit.
  - Stores the completed closed-trade payload for replay after retry, restart or another instance.
  - Preserves the first `COMPLETED` payload under the database row lock; later competing finalizers cannot overwrite it.
  - Uses database uniqueness and row locking for one close intent per trade.

- `app/services/durable_trade_management.py`
  - Fails closed when durable persistence is unavailable.
  - Queries the deterministic Binance Demo close identity before submission and on retry.
  - Persists exchange evidence before income reconciliation.
  - Completes the durable intent before storing a final trade result.
  - Stores and returns only the authoritative winning `COMPLETED` payload.
  - Replays a completed outcome without issuing another close order.
  - Marks exchange-success/finalization-failure paths as recovery-required without downgrading a completed intent.

- `app/api/v1/manual_close_dependencies.py`
  - Builds the production close mutation service from the current durable repository boundary.

- `app/api/v1/routes/trade_management.py`
  - Routes manual close mutations only through the durable service.

## Verification

- `tests/unit/test_be_12_durable_manual_close.py`
  - First close persists one intent, one database-safe exchange-order row and one completed outcome.
  - Verifies the close-order row identity is exactly 64 characters.
  - Repeated request replays the same result without another exchange call.
  - Restart recovery queries the existing filled order and does not resubmit.
  - A second process instance replays the durable completed trade.
  - A competing completion attempt cannot overwrite the first authoritative completed payload.
  - Missing persistence fails closed.

- `tests/unit/test_be_12_authoritative_finalizer.py`
  - Simulates a competing finalizer returning a different durable winner.
  - Proves the locally calculated losing payload is never stored or returned.
  - Proves process state, persistent trade state and API response converge on the same winner.

- `tests/integration/test_trade_management_api.py`
  - Confirms the close route uses the durable mutation dependency.

## Post-merge corrections

PR #50 and PR #51 were merged before all P1 review findings were resolved. BE-12 remains the current serial task until the final authoritative-finalizer corrective PR is merged with green CI.

## Scope boundary

BE-13 partial close, Stop Loss and Take Profit lifecycle verification is not included.
