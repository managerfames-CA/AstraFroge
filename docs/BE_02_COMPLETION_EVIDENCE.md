# BE-02 Completion Evidence

Completion candidate date/time: **2026-07-19 19:03:50 BDT (UTC+06:00)**

## Requirement

Persist idempotency and replay protection across restart and multi-instance deployment.

## Audited implementation

The repository already contains the required durable implementation:

- `mutation_replay_keys` is a database-backed registry keyed by the SHA-256 hash of the operator-supplied idempotency key.
- Each claim persists its request fingerprint, protected action, claim time and expiry time.
- PostgreSQL and SQLite inserts use conflict-safe single-winner semantics.
- Expired claims are pruned or atomically recycled.
- The application wires `MutationReplayGuard` to `TradingStateRepositories` whenever persistence is configured.
- Staging and production require durable database persistence; the bounded process-memory fallback is limited to non-production development/test operation.
- Duplicate requests return replay detection, while reuse of one key for a different request returns a distinct conflict result.

Primary implementation surfaces:

- `migrations/versions/20260717_0002_durable_mutation_replay.py`
- `app/persistence/models.py`
- `app/persistence/repositories.py`
- `app/core/security.py`
- application startup wiring in `app/main.py`

## BE-02-specific verification

`tests/unit/test_be_02_replay_persistence.py` verifies:

1. A claim remains blocked after the first persistence/application instance is closed and a new instance starts against the same database.
2. Two separate application repository/guard instances sharing one database cannot both accept the same idempotency key.
3. The second instance classifies an identical request as `REPLAY`.
4. The second instance classifies the same key with a different request fingerprint/action as `REUSED_FOR_DIFFERENT_REQUEST`.

The existing security and persistence suites additionally cover validation, capacity fail-closed behavior, exact stored hashes, expiry and mutation endpoint responses.

## Scope boundary

BE-02 does not claim continuous Binance order/position reconciliation. That work begins at BE-03 and remains unchecked.

## Status

Implementation and item-specific audit are complete on the BE-02 branch. Final completion requires this PR's required CI to pass and the owner to merge it into `main`.
