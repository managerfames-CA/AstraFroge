# BE-10 Completion Evidence

## Locked requirement

**BE-10:** Include actual commissions and funding in closed-trade performance.

## Implementation

- Added a read-only `JournalCostVerificationService` over Binance Demo income history.
- Attributes exact `COMMISSION` records to verified entry/close fill identities when `tradeId` is available.
- Attributes `FUNDING_FEE` records inside the verified trade lifecycle window.
- Requires unique transaction identities and USDT-denominated cost records.
- Verifies Binance `REALIZED_PNL` income parity against BE-09 verified-fill gross PnL.
- Calculates net realized PnL as:

```text
verified-fill gross realized PnL
+ actual commission income
+ actual funding income
= net realized PnL
```

- Rejects missing commission, malformed values, unsupported assets, positive commission signs, mismatched realized PnL and ambiguous cross-trade income attribution.
- Journal and performance fields no longer use process-stored commission, funding or realized-PnL values as authority.

## API truth

Journal entries expose:

- gross verified-fill PnL;
- actual commission and funding amounts;
- net realized PnL;
- commission and funding transaction identities;
- `VERIFIED_FILLS_NET_ACTUAL_COSTS` source metadata.

Performance summaries aggregate gross PnL, commission, funding and net PnL separately.

## Verification coverage

Focused tests cover:

- actual commission and funding aggregation;
- optional zero funding;
- net PnL calculation;
- process-stored cost values being ignored;
- missing commission fail-closed behavior;
- realized-PnL parity mismatch;
- unsupported cost asset;
- invalid commission sign;
- unrelated fill attribution rejection;
- duplicate income identities;
- malformed timestamps;
- cross-trade double-count rejection.

## Scope boundary

BE-11 Active Trades exchange-authority work is not included.

## Merge gate

BE-10 is complete only after the focused implementation is merged through an owner-approved PR with Backend CI green.
