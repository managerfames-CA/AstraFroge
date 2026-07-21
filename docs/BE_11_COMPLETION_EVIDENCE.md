# BE-11 Completion Evidence

## Requirement

Verify Active Trades from exchange-authoritative positions rather than process-only state.

## Implementation

- `app/services/active_trade_authority.py`
  - Refreshes the Binance Demo account snapshot before publishing open trades.
  - Requires exact symbol, direction and executed-quantity parity.
  - Detects missing, duplicate, orphan, malformed and mismatched positions.
  - Returns no open records when position verification is incomplete.
  - Uses exchange entry price, quantity and unrealized PnL in verified records.

- `app/schemas/execution.py`
  - Adds position verification and snapshot metadata to trade records.

- `app/schemas/trade_management.py`
  - Adds source state, counts, rejection codes and snapshot metadata.

- `app/api/v1/routes/trade_management.py`
  - Read endpoints use the new Active Trades authority.
  - The existing close operation remains separate.

## Tests

- `tests/unit/test_be_11_active_trade_authority.py`
  - Verified match and exchange economics.
  - Status summary.
  - Direction and quantity mismatch.
  - Missing and orphan positions.
  - Unavailable snapshot and duplicate local symbol.

- `tests/integration/test_trade_management_api.py`
  - Active Trades source metadata and verified-position API contract.

## Completion gate

Complete only after final Backend CI passes and the owner merges the PR.
