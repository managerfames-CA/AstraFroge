# BACKEND-V1-005 Scanner Owner Approval

Status: **APPROVED — RULES LOCKED FOR README/CHECKLIST ONLY**

Owner instruction: Scanner questionnaire is approved. Do not start Scanner code yet. First lock the approved rules in README and checklist.

## Approved Setup Catalogue

The approved setup names are:

- Trend Pullback
- Breakout Retest
- EMA Rejection
- Liquidity Sweep Reversal
- Continuation Setup

## Approved Grade Bands

- A+ = 90–100
- A = 85–89
- B+ = 80–84
- Reject = below 80

## Approved Qualification Rule

- A+ and A may become qualified signal candidates only after all approved gates pass.
- B+ is Watch/Near Setup only.
- Reject is hidden from candidate output and retained only as auditable rejection evidence.

## Approved Timeframe Alignment

- `1H` bullish trend allows LONG-only evaluation.
- `1H` bearish trend allows SHORT-only evaluation.
- `1H` range or sideways state is rejected.
- `15M` setup confirmation is required before a candidate can qualify.
- `5M` entry readiness controls readiness; not-ready states remain Watch/Near rather than qualified.

## Approved Scanner Cadence

- Scanner starts OFF after every process restart.
- `START` triggers an immediate scan.
- While enabled, the recurring scan interval is every four hours.
- `STOP` disables recurring scans.
- `RUN NOW` performs a one-time manual scan without requiring the recurring scanner to be enabled.
- Overlapping scans are prohibited.

## Approved Output Contract

Scanner output may include:

- symbol
- direction
- setup name
- grade
- score
- confidence
- timeframe states
- accepted reasons
- rejection reasons
- stale status
- warm-up status

## Coding Gate

This approval locks the README/checklist rules only. Scanner implementation still requires a separate code PR.
