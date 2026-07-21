# BE-08 Completion Evidence

## Locked requirement

Build Journal records only from verified exchange fills, orders and income records.

## Completion candidate

- Date/time: **2026-07-19 20:37:37 BDT**
- Branch: `be-08-verified-exchange-journal`
- Final PR, merge commit and CI evidence: pending owner-approved merge path

## Audit finding

The previous Journal service projected closed `DemoTradeRecord` objects directly from Trade Management. Although the manual-close path already verified a Binance Demo close response and queried income history, the Journal read path did not independently prove the entry order, close order, entry/close fills and income records before publishing a Journal entry.

## BE-08 implementation

- Added the read-only Binance Demo `/fapi/v1/userTrades` adapter for bounded symbol fill history.
- Added `JournalExchangeVerificationService` as the Journal admission boundary.
- Requires a closed durable trade and a configured Binance Demo private read client.
- Verifies the entry client/exchange order identity, terminal filled status and executed quantity.
- Resolves manual closes through the deterministic regular close client ID.
- Resolves Stop Loss and Take Profit closes through the protective Algo identity and actual exchange order identity when available.
- Verifies unique entry and close fill identities and exact summed quantities from Binance Demo user trades.
- Verifies bounded, unique income transaction identities and requires a realized-PnL income record.
- Rejects truncated, malformed, duplicate, incomplete, process-only or unavailable source evidence.
- Journal and performance responses now expose candidate, verified and rejected counts plus stable rejection codes.
- Journal entries expose the verified order, fill and income identities used for admission.
- Existing stored PnL, commission and funding values are not recalculated in BE-08; BE-09 and BE-10 remain separate locked items.

## Focused verification

- Verified Take Profit and manual-close records are admitted with order/fill/income evidence.
- Open or incomplete trades are rejected.
- Missing private API configuration publishes no process-only Journal records.
- Entry order identity, status and quantity mismatches are rejected.
- Missing, duplicate, malformed or quantity-mismatched fills are rejected.
- Missing, duplicate or malformed income evidence is rejected.
- Fill and income windows at the exchange result limit are rejected as potentially truncated.
- Binance Demo `userTrades` path and bounded request parameters have adapter tests.

Focused tests:

- `tests/unit/test_be_08_journal_exchange_verification.py`
- `tests/unit/test_journal_performance.py`
- `tests/unit/test_recovery_demo_client.py`

## Excluded

- BE-09 realized-PnL calculation from verified fills.
- BE-10 commission and funding calculation for closed-trade performance.
- BE-11 exchange-authoritative Active Trades.
- Live or real-money execution.
