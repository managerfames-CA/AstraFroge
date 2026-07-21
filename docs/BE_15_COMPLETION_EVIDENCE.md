# BE-15 Completion Evidence

## Checklist item

**BE-15:** Add strategy, symbol, daily, weekly and monthly performance reporting from verified closed trades.

## Completion time

2026-07-20 18:57 BDT

## Merge path

- Implementation PR: #58
- Implementation merge commit: `3a93bc45bf797c1f725f78ba129d7ec5f2beeac9`
- Post-merge OpenAPI contract repair PR: #59
- Repair merge commit: `134547cd9ebf15f5753b8240292c9392db88337a`

## Implemented authority

- Added typed performance-reporting contracts.
- Added `VerifiedPerformanceReportingService`.
- Added `GET /api/v1/journal-performance/reports?lookback_days=<1..365>`.
- Reports strategy, symbol, UTC daily, ISO Monday-start weekly and UTC calendar-month performance.
- Aggregates verified net realized PnL, verified gross realized PnL, actual commission, actual funding, win/loss/breakeven counts, win rate, average win/loss and best/worst trade.
- Includes only Journal entries with both `source_verified=true` and `actual_costs_verified=true`.
- Preserves Journal source state, candidate count, verified count, rejected count and rejection codes.
- Uses deterministic ordering and Decimal-safe arithmetic.

## Verification evidence

Backend CI run `29743978718` / run #412 passed on repair head `210abfe6bf85639ae994a41f2757c763daf9c3c9` before PR #59 merge.

The successful workflow covered:

- Ruff
- strict Mypy
- full Pytest suite with coverage enforcement
- FastAPI import smoke verification
- container build

Focused BE-15 tests verify all five reporting dimensions and prove that unverified or actual-cost-unverified records are excluded.

## Safety statement

BE-15 adds read-only reporting only. It does not enable live trading, alter Binance Demo mutation behavior, or create a competing PnL source. Verified Journal records remain the sole reporting input.
