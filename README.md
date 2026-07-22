# AstraForge Crypto Backend

Production-oriented FastAPI backend for the **AstraForge Binance USD-M Futures Demo intraday trading system**.

Frontend repository: `zahirulca24-bit/AstraForge-Crypto-Frontend`

## Backend Master Control Checklist per AGENTS.md

### P0 — Durability and Exchange Truth
- [x] BE-01: Add durable persistence for Signals, Risk decisions, orders, fills, positions and trades. (Evidence: [docs/BE_01_COMPLETION_EVIDENCE.md](docs/BE_01_COMPLETION_EVIDENCE.md))
- [x] BE-02: Persist idempotency and replay protection across restart and multi-instance deployment. (Evidence: [docs/BE_02_COMPLETION_EVIDENCE.md](docs/BE_02_COMPLETION_EVIDENCE.md))
- [x] BE-03: Reconcile Binance Demo orders continuously. (Evidence: [docs/BE_03_COMPLETION_EVIDENCE.md](docs/BE_03_COMPLETION_EVIDENCE.md))
- [x] BE-04: Reconcile Binance Demo positions continuously. (Evidence: [docs/BE_04_COMPLETION_EVIDENCE.md](docs/BE_04_COMPLETION_EVIDENCE.md))
- [x] BE-05: Detect partial fills, external closes, missing protective orders and exchange/runtime mismatches. (Evidence: [docs/BE_05_COMPLETION_EVIDENCE.md](docs/BE_05_COMPLETION_EVIDENCE.md))
- [x] BE-06: Recover open orders and positions after restart or deployment. (Evidence: [docs/BE_06_COMPLETION_EVIDENCE.md](docs/BE_06_COMPLETION_EVIDENCE.md))
- [x] BE-07: Fail closed whenever reconciliation cannot prove a safe exchange state. (Evidence: [docs/BE_07_COMPLETION_EVIDENCE.md](docs/BE_07_COMPLETION_EVIDENCE.md))
- [x] BE-08: Build Journal records only from verified exchange fills, orders and income records. (Evidence: [docs/BE_08_COMPLETION_EVIDENCE.md](docs/BE_08_COMPLETION_EVIDENCE.md))
- [x] BE-09: Calculate realized PnL using verified fills. (Evidence: [docs/BE_09_COMPLETION_EVIDENCE.md](docs/BE_09_COMPLETION_EVIDENCE.md))
- [x] BE-10: Include actual commissions and funding in closed-trade performance. (Evidence: [docs/BE_10_COMPLETION_EVIDENCE.md](docs/BE_10_COMPLETION_EVIDENCE.md))

### P1 — Trade Management and Integration
- [x] BE-11: Verify Active Trades from exchange-authoritative positions rather than process-only state. (Evidence: [docs/BE_11_COMPLETION_EVIDENCE.md](docs/BE_11_COMPLETION_EVIDENCE.md))
- [x] BE-12: Make manual close operations durable and idempotent. (Evidence: [docs/BE_12_COMPLETION_EVIDENCE.md](docs/BE_12_COMPLETION_EVIDENCE.md))
- [x] BE-13: Verify partial close, Stop Loss and Take Profit lifecycle events. (Evidence: [docs/BE_13_COMPLETION_EVIDENCE.md](docs/BE_13_COMPLETION_EVIDENCE.md))
- [x] BE-14: Record exchange order ID, client order ID, requested quantity, executed quantity, average fill price and final status. (Evidence: [docs/BE_14_COMPLETION_EVIDENCE.md](docs/BE_14_COMPLETION_EVIDENCE.md))
- [x] BE-15: Add strategy, symbol, daily, weekly and monthly performance reporting from verified closed trades. (Evidence: [docs/BE_15_COMPLETION_EVIDENCE.md](docs/BE_15_COMPLETION_EVIDENCE.md))
- [x] BE-16: Add notifications for orders, fills, TP/SL, Risk blocks, connection failures and reconciliation mismatches. (Evidence: [docs/BE_16_COMPLETION_EVIDENCE.md](docs/BE_16_COMPLETION_EVIDENCE.md))
- [x] BE-17: Confirm Scanner auto-start behavior is intentional, configurable and safe after deployment restart. (Evidence: [docs/BE_17_COMPLETION_EVIDENCE.md](docs/BE_17_COMPLETION_EVIDENCE.md))
- [x] BE-18: Verify Scanner latest-run summary and degraded-run diagnostics against the frontend contract. (Evidence: [docs/BE_18_COMPLETION_EVIDENCE.md](docs/BE_18_COMPLETION_EVIDENCE.md))
- [x] BE-19: Publish stable typed contracts required by Frontend Signals, Risk, Demo Account, Execution, Active Trades and Journal pages. (Evidence: [docs/BE_19_EXECUTION_STATUS_AUDIT.md](docs/BE_19_EXECUTION_STATUS_AUDIT.md))
- [x] BE-20: Run and pass Ruff on latest main. (Evidence: [docs/BE_20_COMPLETION_EVIDENCE.md](docs/BE_20_COMPLETION_EVIDENCE.md))
- [ ] BE-21: Run and pass strict Mypy on latest main.
- [ ] BE-22: Run and pass the full Pytest suite with the required coverage threshold.
- [x] BE-23: Run and pass FastAPI import smoke verification. (Evidence: [docs/BE_23_COMPLETION_EVIDENCE.md](docs/BE_23_COMPLETION_EVIDENCE.md))
- [ ] BE-24: Run and pass Docker build verification.
- [x] BE-25: Confirm the latest direct main commits have successful GitHub Actions evidence. (Evidence: [docs/BE_25_COMPLETION_EVIDENCE.md](docs/BE_25_COMPLETION_EVIDENCE.md))
- [ ] BE-26: Keep README progress, merged PR status and current task synchronized with repository reality.
- [ ] BE-27: Verify deployed health, market, Scanner, Signal, Risk and Demo read-only endpoints.
- [ ] BE-28: Verify protected mutation authentication and idempotency against the deployed Demo runtime.
- [ ] BE-29: Complete frontend-connected runtime testing without enabling real trading.
- [ ] BE-30: Run a final backend security and production-readiness audit.

## Current Next Action
**Current Next Action**: BE-21: Run and pass strict Mypy on latest main.

## Completion Log

| Item | Description | Evidence Document | Merged Commit / PR | Status |
| :--- | :--- | :--- | :--- | :--- |
| **BE-01** | Add durable persistence for Signals, Risk decisions, orders, fills, positions and trades. | [docs/BE_01_COMPLETION_EVIDENCE.md](docs/BE_01_COMPLETION_EVIDENCE.md) | PR #27 / Commit `89a3de68474964a1866cc822c25cdba7b64829d4` | Completed |
| **BE-02** | Persist idempotency and replay protection across restart and multi-instance deployment. | [docs/BE_02_COMPLETION_EVIDENCE.md](docs/BE_02_COMPLETION_EVIDENCE.md) | Commit `9106e9bb7a94ff93f28627c9ed6aa6f4e8bed3e2` | Completed |
| **BE-03** | Reconcile Binance Demo orders continuously. | [docs/BE_03_COMPLETION_EVIDENCE.md](docs/BE_03_COMPLETION_EVIDENCE.md) | Commit `9106e9bb7a94ff93f28627c9ed6aa6f4e8bed3e2` | Completed |
| **BE-04** | Reconcile Binance Demo positions continuously. | [docs/BE_04_COMPLETION_EVIDENCE.md](docs/BE_04_COMPLETION_EVIDENCE.md) | Commit `9106e9bb7a94ff93f28627c9ed6aa6f4e8bed3e2` | Completed |
| **BE-05** | Detect partial fills, external closes, missing protective orders and exchange/runtime mismatches. | [docs/BE_05_COMPLETION_EVIDENCE.md](docs/BE_05_COMPLETION_EVIDENCE.md) | Commit `9106e9bb7a94ff93f28627c9ed6aa6f4e8bed3e2` | Completed |
| **BE-06** | Recover open orders and positions after restart or deployment. | [docs/BE_06_COMPLETION_EVIDENCE.md](docs/BE_06_COMPLETION_EVIDENCE.md) | Commit `9106e9bb7a94ff93f28627c9ed6aa6f4e8bed3e2` | Completed |
| **BE-07** | Fail closed whenever reconciliation cannot prove a safe exchange state. | [docs/BE_07_COMPLETION_EVIDENCE.md](docs/BE_07_COMPLETION_EVIDENCE.md) | Commit `9106e9bb7a94ff93f28627c9ed6aa6f4e8bed3e2` | Completed |
| **BE-08** | Build Journal records only from verified exchange fills, orders and income records. | [docs/BE_08_COMPLETION_EVIDENCE.md](docs/BE_08_COMPLETION_EVIDENCE.md) | Commit `9106e9bb7a94ff93f28627c9ed6aa6f4e8bed3e2` | Completed |
| **BE-09** | Calculate realized PnL using verified fills. | [docs/BE_09_COMPLETION_EVIDENCE.md](docs/BE_09_COMPLETION_EVIDENCE.md) | Commit `9106e9bb7a94ff93f28627c9ed6aa6f4e8bed3e2` | Completed |
| **BE-10** | Include actual commissions and funding in closed-trade performance. | [docs/BE_10_COMPLETION_EVIDENCE.md](docs/BE_10_COMPLETION_EVIDENCE.md) | Commit `9106e9bb7a94ff93f28627c9ed6aa6f4e8bed3e2` | Completed |
| **BE-11** | Verify Active Trades from exchange-authoritative positions rather than process-only state. | [docs/BE_11_COMPLETION_EVIDENCE.md](docs/BE_11_COMPLETION_EVIDENCE.md) | Commit `9106e9bb7a94ff93f28627c9ed6aa6f4e8bed3e2` | Completed |
| **BE-12** | Make manual close operations durable and idempotent. | [docs/BE_12_COMPLETION_EVIDENCE.md](docs/BE_12_COMPLETION_EVIDENCE.md) | PR #50 & PR #51 / Commit `9106e9bb7a94ff93f28627c9ed6aa6f4e8bed3e2` | Completed |
| **BE-13** | Verify partial close, Stop Loss and Take Profit lifecycle events. | [docs/BE_13_COMPLETION_EVIDENCE.md](docs/BE_13_COMPLETION_EVIDENCE.md) | Commit `9106e9bb7a94ff93f28627c9ed6aa6f4e8bed3e2` | Completed |
| **BE-14** | Record exchange order ID, client order ID, requested quantity, executed quantity, average fill price and final status. | [docs/BE_14_COMPLETION_EVIDENCE.md](docs/BE_14_COMPLETION_EVIDENCE.md) | Commit `9106e9bb7a94ff93f28627c9ed6aa6f4e8bed3e2` | Completed |
| **BE-15** | Add strategy, symbol, daily, weekly and monthly performance reporting from verified closed trades. | [docs/BE_15_COMPLETION_EVIDENCE.md](docs/BE_15_COMPLETION_EVIDENCE.md) | PR #58 & PR #59 / Commit `3a93bc45bf797c1f725f78ba129d7ec5f2beeac9` | Completed |
| **BE-16** | Add notifications for orders, fills, TP/SL, Risk blocks, connection failures and reconciliation mismatches. | [docs/BE_16_COMPLETION_EVIDENCE.md](docs/BE_16_COMPLETION_EVIDENCE.md) | PR #69 / Commit `4d054cb26c4621d39a566526b31859a2371e01d6` | Completed |
| **BE-17** | Confirm Scanner auto-start behavior is intentional, configurable and safe after deployment restart. | [docs/BE_17_COMPLETION_EVIDENCE.md](docs/BE_17_COMPLETION_EVIDENCE.md) | Commit `9106e9bb7a94ff93f28627c9ed6aa6f4e8bed3e2` | Completed |
| **BE-18** | Verify Scanner latest-run summary and degraded-run diagnostics against the frontend contract. | [docs/BE_18_COMPLETION_EVIDENCE.md](docs/BE_18_COMPLETION_EVIDENCE.md) | Commit `9106e9bb7a94ff93f28627c9ed6aa6f4e8bed3e2` | Completed |
| **BE-19** | Publish stable typed contracts required by Frontend Signals, Risk, Demo Account, Execution, Active Trades and Journal pages. | [docs/BE_19_EXECUTION_STATUS_AUDIT.md](docs/BE_19_EXECUTION_STATUS_AUDIT.md) | PR #2 / Commit `77a6c97b23a7a5bbbedeb6338f5e6ab767b9be93` | Completed |
| **BE-20** | Run and pass Ruff on latest main. | [docs/BE_20_COMPLETION_EVIDENCE.md](docs/BE_20_COMPLETION_EVIDENCE.md) | PR #3 / Branch jules-12746788728458930920-74e9f99d | Completed |
| **BE-23** | Run and pass FastAPI import smoke verification. | [docs/BE_23_COMPLETION_EVIDENCE.md](docs/BE_23_COMPLETION_EVIDENCE.md) | PR #4 / Branch jules-be-23-smoke-verification | Completed |
| **BE-25** | Confirm the latest direct main commits have successful GitHub Actions evidence. | [docs/BE_25_COMPLETION_EVIDENCE.md](docs/BE_25_COMPLETION_EVIDENCE.md) | PR #6 / Commit `b7f6c965bea72c094d1dc1e7f8969b72b67c6a06` | Completed |
