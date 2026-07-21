# BE-09 Completion Evidence

## Locked requirement

**BE-09:** Calculate realized PnL using verified fills.

## Implementation evidence

- `app/services/journal_exchange_verification.py`
  - Aggregates every verified entry and close fill by exchange order identity.
  - Requires unique fill IDs, positive finite quantity/price and exact executed-quantity parity.
  - Calculates weighted-average entry and close prices from `sum(quantity × price) / sum(quantity)`.
  - Calculates gross realized PnL from verified fills only:
    - LONG: `(close average − entry average) × verified quantity`
    - SHORT: `(entry average − close average) × verified quantity`
  - Ignores process-stored prices and PnL values as calculation authority.
  - Fails closed on missing, malformed, duplicate, truncated or quantity-mismatched fill evidence.

- `app/services/journal_performance.py`
  - Uses fill-derived gross realized PnL for Journal entries, sorting and performance metrics.
  - Uses verified weighted-average entry/exit prices in Journal output.
  - No longer uses `DemoTradeRecord.realized_pnl_usdt` or `gross_realized_pnl_usdt` as Journal PnL authority.

- `app/schemas/journal_performance.py`
  - Publishes `VERIFIED_FILLS_GROSS` as the typed PnL source.
  - Exposes verified fill quantity and verified-fill-only status flags.

## Verification evidence

- `tests/unit/test_be_09_verified_fill_pnl.py`
  - Multi-fill weighted-average LONG PnL.
  - SHORT direction PnL sign handling.
  - Close quantity mismatch rejection.
  - Non-positive fill-price rejection.
  - Exchange income amount cannot override fill-derived PnL.

- `tests/unit/test_journal_performance.py`
  - Deliberately stores false process PnL/price values and verifies they are ignored.
  - Verifies Journal entries, sorting and performance summary use verified fill economics.

## Scope boundary

- No real-money trading support was added.
- No new exchange mutation was added.
- **BE-10 is excluded:** this change does not recalculate actual commission or funding adjustments. Those remain a separate locked checklist item.

## Merge gate

BE-09 may be marked complete only after the focused implementation is merged through an owner-approved PR with successful Backend CI evidence.
