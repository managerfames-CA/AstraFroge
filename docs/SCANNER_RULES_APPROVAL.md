# BACKEND-V1-005 Scanner Rules Approval

Contract name: **Scanner Contract Version 1**

Owner questionnaire: **APPROVED**

Deterministic Scanner Contract Version 1: **APPROVED**

Scanner implementation: **BLOCKED — CONTRACT VERSION 1 MUST BE PRESENT ON `main` AND A SEPARATE OWNER IMPLEMENTATION TASK MUST BE APPROVED**

This file is the single canonical Scanner governance source for
`BACKEND-V1-005 — Scanner Engine`.

Scanner Contract Version 1 is documentation and decision governance only. It does
not implement Scanner schemas, services, routes, scheduling, persistence, Signal,
Risk, execution, account access, or order placement.

Implementation may begin only after this exact approved contract revision is merged
into `main` and the owner assigns a separate Scanner runtime implementation task.

## Version Governance

- Every formula, threshold, weight, state transition, expiry window, freshness limit,
  and code name in this file belongs to **Scanner Contract Version 1**.
- Later backtesting may justify different thresholds, but runtime behavior must not
  change silently.
- Any change requires an explicit versioned governance change, documentation review,
  owner approval, and a separately verified runtime change.
- Approved setup names and grade bands must not be renamed or reinterpreted without
  owner approval.
- Numeric comparison boundaries are inclusive unless a formula explicitly uses
  strict `<` or `>`.
- Calculations use exact Decimal arithmetic where available.
- Final score and confidence use Decimal `ROUND_HALF_UP` to the nearest integer.
- No implementer discretion is permitted where this contract supplies a formula,
  ordering rule, failure rule, or unavailable-input rule.

## Locked Data and Safety Boundary

- Public source: real Binance USD-M Futures public market data.
- Closed candles only.
- `1H`: primary trend and regime.
- `15M`: setup detection and confirmation.
- `5M`: entry readiness and active-candidate refresh.
- Minimum history: 200 contiguous closed candles on each timeframe.
- Active Universe: current eligible Universe Engine output, maximum 50 symbols.
- Approved Scanner V1 inputs: EMA `20/50/200`, RSI `14`, MACD `12/26/9`,
  ATR `14`, volume SMA `20`, volume ratio, normalized OHLCV, Universe quote
  volume, spread, rank, and freshness metadata.
- Current cumulative VWAP is not a mandatory Scanner V1 gate and is not used in
  Scanner Contract Version 1 scoring.
- Stale data cannot qualify.
- Scanner must not access authenticated account data or place orders.
- No live, Spot, Testnet, Sandbox, local paper-trading, or mock-data fallback.
- Binance USD-M Futures Demo is reserved for a later separately approved execution
  task.

## Approved Setup Catalogue

| Setup name | Setup ID |
|---|---|
| Trend Pullback | `trend_pullback` |
| Breakout Retest | `breakout_retest` |
| EMA Rejection | `ema_rejection` |
| Liquidity Sweep Reversal | `liquidity_sweep_reversal` |
| Continuation Setup | `continuation_setup` |

All five setups permit LONG and SHORT evaluation subject to the mutually exclusive
`1H` directional regime contract.

## Approved Grade and Qualification Policy

- A+ = `90–100`
- A = `85–89`
- B+ = `80–84`
- Reject = below `80`

Qualification rules:

- A+ and A may qualify only when every mandatory gate passes, confidence is at
  least `70`, and exact `5M` entry readiness passes.
- B+ is always `WATCH_NEAR`; B+ is never a qualified signal.
- Confidence `60–69` is always `WATCH_NEAR`.
- Confidence below `60` is Reject.
- Entry-not-ready candidates may be `WATCH_NEAR` only.
- Mandatory data, freshness, trend, volatility, setup-presence, and
  setup-invalidation gates run before scoring.
- A failed mandatory gate produces no valid score or confidence.
- Score cannot convert invalid data, stale data, `SIDEWAYS`, `MIXED`, an absent
  setup, or an invalidated setup into B+, A, or A+.

## Timeframe and Candle Bindings

For every evaluated series, index `0` means the latest closed candle, index `1`
the previous closed candle, and index `3` the candle three positions before index
`0`.

- `Hn`: current `1H` candle and aligned indicator point at index `n` for the
  evaluation run.
- `Sn`: `15M` candle and aligned indicator point at index `n` relative to the
  candidate's stored setup-confirmation candle `S0`. During initial discovery,
  `S0` is the latest closed `15M` candle. During Active Candidate Refresh, the
  stored `S0`, `S1`, `S2`, and `S3` bindings do not move.
- `En`: current `5M` candle and aligned indicator point at index `n` for the
  evaluation run.
- `B`: the stored selected `15M` Breakout Retest breakout candle.
- `direction`: exactly `LONG` or `SHORT`.
- `ATR15`: `S0.atr14` in setup/scoring formulas.
- `ATR5`: `E0.atr14` in entry/scoring formulas.
- For a lifecycle invalidation evaluated on a newer closed candle, the formula
  explicitly uses that current candle's aligned ATR14; it does not reuse the
  reference candle ATR unless the formula names the reference candle.

Every scoring symbol below is bound to one of these exact candles, a stored setup
reference candle, a stored setup level, or current Universe metadata.

## Basic Mathematical Helpers

```text
BODY(c) = abs(c.close - c.open)
RANGE(c) = c.high - c.low
LOWER_WICK(c) = min(c.open, c.close) - c.low
UPPER_WICK(c) = c.high - max(c.open, c.close)

LONG_CLOSE_POSITION(c) = (c.close - c.low) / RANGE(c)
SHORT_CLOSE_POSITION(c) = (c.high - c.close) / RANGE(c)

PRIOR_HIGH(n, candles_before_reference) =
    maximum high of exactly the n closed candles immediately before reference

PRIOR_LOW(n, candles_before_reference) =
    minimum low of exactly the n closed candles immediately before reference

clamp01(x) = min(1, max(0, x))
clamp(x, lower, upper) = min(upper, max(lower, x))
```

Normalization helpers:

```text
N_UP(x, minimum, full) =
    0                              when x <= minimum
    1                              when x >= full
    (x - minimum) / (full-minimum) otherwise

N_DOWN(x, full, maximum) =
    1                              when x <= full
    0                              when x >= maximum
    1 - (x-full)/(maximum-full)    otherwise

N_TARGET(x, target, tolerance) =
    clamp01(1 - abs(x-target)/tolerance)
```

All Version 1 normalization constants have `full > minimum`,
`maximum > full`, and `tolerance > 0`. Violation is a contract/configuration
failure, not an input that an implementer may reinterpret.

## Directional Scoring Helper Contract

Every directional helper is piecewise and accepts only `LONG` or `SHORT`.

```text
DIRECTIONAL_WICK(c, direction) =
    LOWER_WICK(c) when direction == LONG
    UPPER_WICK(c) when direction == SHORT

DIRECTIONAL_CLOSE_POSITION(c, direction) =
    LONG_CLOSE_POSITION(c) when direction == LONG
    SHORT_CLOSE_POSITION(c) when direction == SHORT

DIRECTIONAL_EXTREME(c, direction) =
    c.low when direction == LONG
    c.high when direction == SHORT

DIRECTIONAL_BREAK_MARGIN(price, level, direction) =
    price - level when direction == LONG
    level - price when direction == SHORT

DIRECTIONAL_RECLAIM_MARGIN(close, level, direction) =
    close - level when direction == LONG
    level - close when direction == SHORT

DIRECTIONAL_EMA_EXTENSION(close, ema, direction) =
    close - ema when direction == LONG
    ema - close when direction == SHORT

DIRECTIONAL_HISTOGRAM(histogram, direction) =
    histogram when direction == LONG
    -histogram when direction == SHORT

DIRECTIONAL_DELTA(current, previous, direction) =
    current - previous when direction == LONG
    previous - current when direction == SHORT

DIRECTIONAL_RSI_MARGIN(rsi, direction) =
    rsi - 50 when direction == LONG
    50 - rsi when direction == SHORT

DIRECTIONAL_SWEEP_DEPTH(c, level, direction) =
    level - c.low when direction == LONG
    c.high - level when direction == SHORT

DIRECTIONAL_PREVIOUS_BREAK_LEVEL(E1, direction) =
    E1.high when direction == LONG
    E1.low when direction == SHORT

DIRECTIONAL_COMPRESSION_BOUNDARY(compression_high, compression_low, direction) =
    compression_high when direction == LONG
    compression_low when direction == SHORT
```

A directional margin may be negative before mandatory setup or entry acceptance.
After the corresponding acceptance gate passes, the directional margin used for
scoring must be non-negative.

### Deterministic selected EMA

For EMA Rejection, the evaluated candle is `S0` and ATR is `S0.atr14`.

```text
ema20_absolute_distance =
    abs(DIRECTIONAL_EXTREME(S0,direction) - S0.ema20)

ema50_absolute_distance =
    abs(DIRECTIONAL_EXTREME(S0,direction) - S0.ema50)

ema20_eligible =
    ema20_absolute_distance <= 0.20 × S0.atr14

ema50_eligible =
    ema50_absolute_distance <= 0.20 × S0.atr14
```

Selection order:

1. Calculate both absolute distances.
2. Only an EMA whose distance is `<= 0.20 × S0.atr14` is eligible.
3. If both are eligible, select the EMA with the smaller absolute distance.
4. An exact distance tie selects EMA20.
5. If only one is eligible, select that EMA.
6. If neither is eligible, `selected_ema` is unavailable and EMA Rejection fails
   before scoring.
7. An unavailable `selected_ema` must never enter a scoring formula.

Selected-EMA normalized distance:

```text
SELECTED_EMA_DISTANCE(S0,selected_ema,direction) =
    abs(S0.low - selected_ema) / S0.atr14   when direction == LONG
    abs(S0.high - selected_ema) / S0.atr14  when direction == SHORT
```

### Setup-bound scoring symbols

```text
BREAKOUT_RETEST_EXTREME =
    DIRECTIONAL_EXTREME(S0,direction)

LIQUIDITY_SWEEP_DEPTH =
    DIRECTIONAL_SWEEP_DEPTH(S0,level,direction)

COMPRESSION_BOUNDARY =
    DIRECTIONAL_COMPRESSION_BOUNDARY(
        compression_high,
        compression_low,
        direction
    )

SHARED_PREVIOUS_BREAK_LEVEL =
    DIRECTIONAL_PREVIOUS_BREAK_LEVEL(E1,direction)
```

The setup-specific trigger price is:

```text
Trend Pullback:
    max(E1.high,E2.high) + 0.05 × E0.atr14  for LONG
    min(E1.low,E2.low) - 0.05 × E0.atr14    for SHORT

Breakout Retest:
    level + 0.05 × E0.atr14                 for LONG
    level - 0.05 × E0.atr14                 for SHORT

EMA Rejection:
    S0.high + 0.02 × E0.atr14               for LONG
    S0.low - 0.02 × E0.atr14                for SHORT

Liquidity Sweep Reversal:
    S0.high + 0.02 × E0.atr14               for LONG
    S0.low - 0.02 × E0.atr14                for SHORT

Continuation Setup:
    compression_high + 0.05 × E0.atr14      for LONG
    compression_low - 0.05 × E0.atr14       for SHORT
```

Final entry trigger:

```text
ENTRY_TRIGGER_PRICE =
    max(setup_specific_trigger_price, SHARED_PREVIOUS_BREAK_LEVEL)
        when direction == LONG

    min(setup_specific_trigger_price, SHARED_PREVIOUS_BREAK_LEVEL)
        when direction == SHORT
```

Setup-specific minimum `15M` volume ratio used by the Volume factor:

```text
trend_pullback             = 0.80
breakout_retest            = 0.80
ema_rejection              = 1.00
liquidity_sweep_reversal   = 1.20
continuation_setup         = 1.20
```

For Breakout Retest, the `0.80` value is the `S0` retest-candle minimum; the
separate breakout-candle `B.volume_ratio >= 1.50` remains unchanged.

### Zero and unavailable behavior

- `ATR <= 0` makes every applicable ATR-normalized component unavailable and fails
  the mandatory indicator gate before scoring.
- `RANGE(c) <= 0` makes wick and close-position inputs for candle `c` unavailable.
- `BODY(c) <= 0` makes every wick/body input for candle `c` unavailable.
- Missing `selected_ema` fails EMA Rejection before scoring.
- Missing or non-finite EMA, RSI, MACD, histogram, volume ratio, Universe quote
  volume, spread, or required candle field makes its applicable component
  unavailable.
- An unavailable component after a purported setup acceptance indicates the setup
  was not validly accepted; the evaluation fails closed.
- An unavailable component must not be silently treated as zero, omitted and
  reweighted, copied from another timeframe, or replaced with a fallback.
- No implementer discretion is permitted.

## Mandatory Gate Evaluation Order

Mandatory gates run before scoring in this exact order:

1. Validate Scanner run time authority and current Universe snapshot.
2. Validate availability, continuity, timestamps, OHLCV, closed status, freshness,
   and 200-candle history for `1H`, `15M`, and `5M`.
3. Validate all formula-required indicator values, positive ATR, positive volume
   SMA, and valid market structure.
4. Evaluate the mutually exclusive `1H` regime.
5. Reject `SIDEWAYS`, `MIXED`, or direction mismatch.
6. Evaluate one complete approved deterministic `15M` setup formula.
7. Reject an absent or already invalidated setup.
8. Evaluate normalized volatility.
9. Validate every setup-bound scoring symbol, including `selected_ema` when
   applicable.
10. Only after steps 1–9 pass may score and confidence be calculated.
11. Qualification additionally requires `5M` entry readiness, effective score at
    least `85`, confidence at least `70`, and grade A or A+.

A mandatory-gate failure records rejection/audit evidence and leaves score and
confidence unavailable. It must not be represented as score `0`.

## Scanner Cadence Contract

### Full Universe Scan

- `START` changes Scanner from `OFF` to `ON` and immediately attempts a Full
  Universe Scan.
- While `ON`, the next Full Universe Scan is due exactly four hours after the prior
  Full Universe Scan `run_started_at`.
- `RUN NOW` immediately attempts one Full Universe Scan.
- `RUN NOW` does not change `ON/OFF`.
- `RUN NOW` while `ON` resets the next full scan to four hours after that manual
  run's `run_started_at`.
- `RUN NOW` while `OFF` enables neither recurring full scans nor active refresh.
- Only a Full Universe Scan may create a candidate from a new valid `15M` setup.

### Active Candidate Refresh

- While `ON`, refresh is due every five minutes on exchange-time five-minute
  boundaries.
- Entry-readiness evaluation applies only to existing `DETECTED` and `WATCH_NEAR`.
- Refresh checks current `5M` readiness, mandatory freshness, setup/trend
  invalidation, and expiry.
- Refresh does not rescan the full Universe.
- Refresh cannot create a candidate without a valid Full Universe Scan `15M`
  setup record.
- `QUALIFIED` candidates are not re-tested for the original entry trigger and are
  not rescored for downgrade. Only deterministic invalidation and fixed
  qualification expiry are maintained.
- Refresh stops when Scanner changes to `OFF`.

### Shared non-overlapping lock

- Full Universe Scan and Active Candidate Refresh use one exclusive lock.
- A second attempt while locked is not queued.
- It records `SCAN_ALREADY_RUNNING` and is skipped.
- The active run continues.
- A skipped five-minute refresh waits until the next boundary and is not backfilled.
- `STOP` disables future scheduled attempts.
- Restart clears in-memory lifecycle records and leaves Scanner `OFF`.

## Mutually Exclusive `1H` Regime Contract

Mandatory `1H` gates run first.

```text
SIDEWAYS =
    H0.structure.state == range
    OR abs(H0.ema20-H0.ema50) <= 0.25 × H0.atr14
    OR (
        45 <= H0.rsi14 <= 55
        AND abs(H0.macd_histogram) <= 0.05 × H0.atr14
    )
```

```text
BULLISH =
    SIDEWAYS == false
    AND H0.close > H0.ema20 > H0.ema50 > H0.ema200
    AND H0.ema20 > H3.ema20
    AND H0.ema50 > H3.ema50
    AND H0.ema200 >= H3.ema200
    AND H0.structure.state == bullish
    AND 55 <= H0.rsi14 <= 80
    AND H0.macd > H0.macd_signal
    AND H0.macd_histogram > 0
```

```text
BEARISH =
    SIDEWAYS == false
    AND H0.close < H0.ema20 < H0.ema50 < H0.ema200
    AND H0.ema20 < H3.ema20
    AND H0.ema50 < H3.ema50
    AND H0.ema200 <= H3.ema200
    AND H0.structure.state == bearish
    AND 20 <= H0.rsi14 <= 45
    AND H0.macd < H0.macd_signal
    AND H0.macd_histogram < 0
```

Evaluation order:

1. Calculate `SIDEWAYS`.
2. Calculate `BULLISH` only when `SIDEWAYS == false`.
3. Calculate `BEARISH` only when `SIDEWAYS == false`.
4. `SIDEWAYS == true` → `SIDEWAYS`.
5. Else `BULLISH == true` → `BULLISH`.
6. Else `BEARISH == true` → `BEARISH`.
7. Else → `MIXED`.

The states are mutually exclusive. `SIDEWAYS` and `MIXED` reject qualification.
`BULLISH` permits LONG only; `BEARISH` permits SHORT only.

## Normalized Volatility Contract

```text
15M valid: 0.0015 <= S0.atr14 / S0.close <= 0.025
5M valid:  0.0005 <= E0.atr14 / E0.close <= 0.015
```

Below a lower limit is `VOLATILITY_BELOW_MINIMUM`; above an upper limit is
`VOLATILITY_ABOVE_MAXIMUM`.

## Shared `5M` Entry-Readiness Contract

### LONG

```text
E0.close > E0.ema20
AND E0.ema20 >= E0.ema50
AND E0.close > E1.high
AND E0.close > E0.open
AND LONG_CLOSE_POSITION(E0) >= 0.65
AND 52 <= E0.rsi14 <= 72
AND E0.macd > E0.macd_signal
AND E0.macd_histogram > 0
AND E0.macd_histogram >= E1.macd_histogram
AND E0.volume_ratio >= 1.10
AND E0.close - E0.ema20 <= 0.75 × E0.atr14
```

### SHORT

```text
E0.close < E0.ema20
AND E0.ema20 <= E0.ema50
AND E0.close < E1.low
AND E0.close < E0.open
AND SHORT_CLOSE_POSITION(E0) >= 0.65
AND 28 <= E0.rsi14 <= 48
AND E0.macd < E0.macd_signal
AND E0.macd_histogram < 0
AND E0.macd_histogram <= E1.macd_histogram
AND E0.ema20 - E0.close <= 0.75 × E0.atr14
AND E0.volume_ratio >= 1.10
```

The qualifying `E0.close_time` must be later than `setup_confirmed_at`, and `5M`
volatility must pass.

## Setup Contract 1 — Trend Pullback

### LONG `15M`

```text
1H regime == BULLISH
AND S3.close > S2.close > S1.close
AND S1.low <= S1.ema20
AND S1.high >= S1.ema50
AND S1.close >= S1.ema50 - 0.25 × S1.atr14
AND S0.close > S0.open
AND S0.close > S0.ema20
AND LONG_CLOSE_POSITION(S0) >= 0.65
AND 48 <= S0.rsi14 <= 65
AND S0.rsi14 > S1.rsi14
AND S0.macd_histogram > S1.macd_histogram
AND S0.volume_ratio >= 0.80
```

### SHORT `15M`

```text
1H regime == BEARISH
AND S3.close < S2.close < S1.close
AND S1.high >= S1.ema20
AND S1.low <= S1.ema50
AND S1.close <= S1.ema50 + 0.25 × S1.atr14
AND S0.close < S0.open
AND S0.close < S0.ema20
AND SHORT_CLOSE_POSITION(S0) >= 0.65
AND 35 <= S0.rsi14 <= 52
AND S0.rsi14 < S1.rsi14
AND S0.macd_histogram < S1.macd_histogram
AND S0.volume_ratio >= 0.80
```

Entry trigger is the Trend Pullback setup-specific trigger defined in the
Directional Scoring Helper Contract. `E0.close` must pass that trigger and the
shared `5M` contract.

Invalidation:

```text
LONG:
    closed 15M close < EMA50 - 0.25 × ATR15
    OR closed 5M close < pullback_swing_low - 0.10 × ATR5

SHORT:
    closed 15M close > EMA50 + 0.25 × ATR15
    OR closed 5M close > pullback_swing_high + 0.10 × ATR5

pullback_swing_low = min(S3.low,S2.low,S1.low,S0.low)
pullback_swing_high = max(S3.high,S2.high,S1.high,S0.high)
```

Reference and `setup_confirmed_at`: `S0.close_time`.
Expiry: `S0.close_time + 60 minutes`.

## Setup Contract 2 — Breakout Retest

`B` is the most recent valid candle in search order `S1`, `S2`, then `S3`.
The level uses exactly the 20 closed `15M` candles immediately before `B`;
`B` is excluded.

### LONG `15M`

```text
1H regime == BULLISH
AND level = PRIOR_HIGH(20,candles immediately before B)
AND B.close >= level + 0.10 × B.atr14
AND BODY(B) >= 0.50 × B.atr14
AND LONG_CLOSE_POSITION(B) >= 0.70
AND B.volume_ratio >= 1.50
AND abs(S0.low-level) <= 0.20 × S0.atr14
AND S0.close >= level + 0.05 × S0.atr14
AND S0.volume_ratio >= 0.80
```

### SHORT `15M`

```text
1H regime == BEARISH
AND level = PRIOR_LOW(20,candles immediately before B)
AND B.close <= level - 0.10 × B.atr14
AND BODY(B) >= 0.50 × B.atr14
AND SHORT_CLOSE_POSITION(B) >= 0.70
AND B.volume_ratio >= 1.50
AND abs(S0.high-level) <= 0.20 × S0.atr14
AND S0.close <= level - 0.05 × S0.atr14
AND S0.volume_ratio >= 0.80
```

Entry trigger uses the exact Breakout Retest setup-specific and final trigger
definitions in the helper contract.

Invalidation:

```text
LONG: closed 15M close < level - 0.15 × ATR15
SHORT: closed 15M close > level + 0.15 × ATR15
```

Reference: `B.close_time`.
`setup_confirmed_at`: `S0.close_time`.
Expiry: `B.close_time + 120 minutes`.

## Setup Contract 3 — EMA Rejection

`selected_ema` is selected only by the deterministic selected-EMA contract above.

### LONG `15M`

```text
1H regime == BULLISH
AND selected_ema is available
AND S0.low <= selected_ema
AND selected_ema - S0.low <= 0.20 × S0.atr14
AND S0.close >= selected_ema + 0.05 × S0.atr14
AND BODY(S0) >= 0.10 × S0.atr14
AND LOWER_WICK(S0) / BODY(S0) >= 1.50
AND LONG_CLOSE_POSITION(S0) >= 0.70
AND S0.volume_ratio >= 1.00
AND 45 <= S0.rsi14 <= 65
AND S0.macd_histogram >= S1.macd_histogram
```

### SHORT `15M`

```text
1H regime == BEARISH
AND selected_ema is available
AND S0.high >= selected_ema
AND S0.high - selected_ema <= 0.20 × S0.atr14
AND S0.close <= selected_ema - 0.05 × S0.atr14
AND BODY(S0) >= 0.10 × S0.atr14
AND UPPER_WICK(S0) / BODY(S0) >= 1.50
AND SHORT_CLOSE_POSITION(S0) >= 0.70
AND S0.volume_ratio >= 1.00
AND 35 <= S0.rsi14 <= 55
AND S0.macd_histogram <= S1.macd_histogram
```

Entry trigger uses the exact EMA Rejection setup-specific and final trigger
definitions in the helper contract.

Invalidation:

```text
LONG:
    closed price < selected_ema - 0.20 × ATR15
    OR closed price < S0.low - 0.05 × ATR15

SHORT:
    closed price > selected_ema + 0.20 × ATR15
    OR closed price > S0.high + 0.05 × ATR15
```

Reference and `setup_confirmed_at`: `S0.close_time`.
Expiry: `S0.close_time + 45 minutes`.

## Setup Contract 4 — Liquidity Sweep Reversal

### LONG `15M`

```text
1H regime == BULLISH
AND level = PRIOR_LOW(10,S1..S10)
AND S0.low <= level - 0.05 × S0.atr14
AND S0.close >= level + 0.05 × S0.atr14
AND BODY(S0) > 0
AND LOWER_WICK(S0) / BODY(S0) >= 1.50
AND LONG_CLOSE_POSITION(S0) >= 0.70
AND S0.volume_ratio >= 1.20
AND 35 <= S0.rsi14 <= 55
AND S0.rsi14 > S1.rsi14
AND S0.macd_histogram > S1.macd_histogram
```

### SHORT `15M`

```text
1H regime == BEARISH
AND level = PRIOR_HIGH(10,S1..S10)
AND S0.high >= level + 0.05 × S0.atr14
AND S0.close <= level - 0.05 × S0.atr14
AND BODY(S0) > 0
AND UPPER_WICK(S0) / BODY(S0) >= 1.50
AND SHORT_CLOSE_POSITION(S0) >= 0.70
AND S0.volume_ratio >= 1.20
AND 45 <= S0.rsi14 <= 65
AND S0.rsi14 < S1.rsi14
AND S0.macd_histogram < S1.macd_histogram
```

Entry trigger uses the exact Liquidity Sweep setup-specific and final trigger
definitions in the helper contract.

Invalidation:

```text
LONG: closed price < S0.low - 0.05 × ATR on evaluated timeframe
SHORT: closed price > S0.high + 0.05 × ATR on evaluated timeframe
```

Reference and `setup_confirmed_at`: `S0.close_time`.
Expiry: `S0.close_time + 45 minutes`.

## Setup Contract 5 — Continuation Setup

Compression candles are `S3`, `S2`, and `S1`.

```text
compression_width =
    max(S3.high,S2.high,S1.high) - min(S3.low,S2.low,S1.low)

compression_high = max(S3.high,S2.high,S1.high)
compression_low = min(S3.low,S2.low,S1.low)
```

### LONG `15M`

```text
1H regime == BULLISH
AND each S3,S2,S1 close > its EMA20
AND each S3,S2,S1 EMA20 > EMA50
AND RANGE(each S3,S2,S1) <= 0.90 × its ATR14
AND compression_width <= 1.50 × S0.atr14
AND average(volume_ratio S3,S2,S1) <= 1.10
AND S0.close >= compression_high + 0.05 × S0.atr14
AND BODY(S0) >= 0.40 × S0.atr14
AND LONG_CLOSE_POSITION(S0) >= 0.70
AND S0.volume_ratio >= 1.20
AND 55 <= S0.rsi14 <= 70
AND S0.macd_histogram > 0
AND S0.macd_histogram >= S1.macd_histogram
```

### SHORT `15M`

```text
1H regime == BEARISH
AND each S3,S2,S1 close < its EMA20
AND each S3,S2,S1 EMA20 < EMA50
AND RANGE(each S3,S2,S1) <= 0.90 × its ATR14
AND compression_width <= 1.50 × S0.atr14
AND average(volume_ratio S3,S2,S1) <= 1.10
AND S0.close <= compression_low - 0.05 × S0.atr14
AND BODY(S0) >= 0.40 × S0.atr14
AND SHORT_CLOSE_POSITION(S0) >= 0.70
AND S0.volume_ratio >= 1.20
AND 30 <= S0.rsi14 <= 45
AND S0.macd_histogram < 0
AND S0.macd_histogram <= S1.macd_histogram
```

Entry trigger uses the exact Continuation setup-specific and final trigger
definitions in the helper contract.

Invalidation:

```text
LONG: closed 15M close < compression_high - 0.15 × ATR15
SHORT: closed 15M close > compression_low + 0.15 × ATR15
```

Reference and `setup_confirmed_at`: `S0.close_time`.
Expiry: `S0.close_time + 45 minutes`.

## Exact Setup and Entry Contracts

```text
15M_SETUP_CONFIRMED =
    all mandatory 1H and 15M gates pass
    AND regime is BULLISH or BEARISH
    AND one complete approved setup formula is true
    AND setup invalidation is false
    AND 15M normalized volatility is valid
```

```text
5M_ENTRY_READY =
    all mandatory 5M data and freshness gates pass
    AND shared directional 5M formula passes
    AND direction-specific E0.close passes ENTRY_TRIGGER_PRICE
    AND 5M normalized volatility is valid
    AND E0.close_time > setup_confirmed_at
    AND candidate expiry has not been reached
```

Direction-specific trigger pass:

```text
LONG: E0.close > ENTRY_TRIGGER_PRICE
SHORT: E0.close < ENTRY_TRIGGER_PRICE
```

## Scanner Score Contract

Score measures opportunity quality and ranking, not data validity. Mandatory failures
produce no score.

### Score factors and weights

| Factor | Weight |
|---|---:|
| `1H` trend quality | 20 |
| `15M` setup quality | 25 |
| `5M` entry-readiness quality | 20 |
| Momentum quality | 15 |
| Volume quality | 10 |
| Universe liquidity quality | 5 |
| Freshness margin | 5 |
| **Total** | **100** |

### `1H` trend quality — 20

All inputs are `1H`.

```text
ema_separation =
    N_UP(abs(H0.ema20-H0.ema50)/H0.atr14, 0.25, 1.00)

ema50_slope =
    N_UP(
        DIRECTIONAL_DELTA(H0.ema50,H3.ema50,direction)/H0.atr14,
        0,
        0.75
    )

ema200_distance =
    N_UP(
        DIRECTIONAL_BREAK_MARGIN(H0.close,H0.ema200,direction)/H0.atr14,
        0,
        3.00
    )

rsi_margin =
    N_UP(DIRECTIONAL_RSI_MARGIN(H0.rsi14,direction), 5, 15)

trend_points =
    7 × ema_separation
  + 5 × ema50_slope
  + 4 × ema200_distance
  + 4 × rsi_margin
```

### `15M` setup quality — 25

#### Trend Pullback

All inputs are `15M`.

```text
zone_low = min(S1.ema20,S1.ema50)
zone_high = max(S1.ema20,S1.ema50)

distance_to_zone =
    0
        when zone_low <= S1.close <= zone_high
    min(abs(S1.close-zone_low),abs(S1.close-zone_high))
        otherwise

zone_precision =
    N_DOWN(distance_to_zone/S1.atr14, 0, 0.25)

recovery_body =
    N_UP(BODY(S0)/S0.atr14, 0.10, 1.00)

rsi_recovery =
    N_UP(
        DIRECTIONAL_DELTA(S0.rsi14,S1.rsi14,direction),
        0,
        10
    )

histogram_recovery =
    N_UP(
        DIRECTIONAL_DELTA(
            S0.macd_histogram,
            S1.macd_histogram,
            direction
        ) / S0.atr14,
        0,
        0.10
    )

setup_points =
    8.75 × zone_precision
  + 6.25 × recovery_body
  + 5.00 × rsi_recovery
  + 5.00 × histogram_recovery
```

#### Breakout Retest

`B`, `level`, and `S0` are the accepted `15M` setup values.

```text
breakout_distance =
    N_UP(
        DIRECTIONAL_BREAK_MARGIN(B.close,level,direction)/B.atr14,
        0.10,
        0.40
    )

retest_precision =
    N_DOWN(
        abs(BREAKOUT_RETEST_EXTREME-level)/S0.atr14,
        0,
        0.20
    )

hold_margin =
    N_UP(
        DIRECTIONAL_RECLAIM_MARGIN(S0.close,level,direction)/S0.atr14,
        0.05,
        0.40
    )

breakout_volume =
    N_UP(B.volume_ratio, 1.50, 2.50)

setup_points =
    6.25 × breakout_distance
  + 7.50 × retest_precision
  + 6.25 × hold_margin
  + 5.00 × breakout_volume
```

#### EMA Rejection

All inputs are accepted `15M` `S0` values.

```text
wick_ratio =
    DIRECTIONAL_WICK(S0,direction) / BODY(S0)

ema_distance =
    SELECTED_EMA_DISTANCE(S0,selected_ema,direction)

close_position =
    DIRECTIONAL_CLOSE_POSITION(S0,direction)

setup_points =
    7.50 × N_UP(wick_ratio, 1.50, 4.00)
  + 7.50 × N_DOWN(ema_distance, 0, 0.20)
  + 6.25 × N_UP(close_position, 0.70, 0.90)
  + 3.75 × N_UP(S0.volume_ratio, 1.00, 2.50)
```

#### Liquidity Sweep Reversal

All inputs are accepted `15M` `S0` and `level`.

```text
sweep_depth =
    N_UP(LIQUIDITY_SWEEP_DEPTH/S0.atr14, 0.05, 0.40)

reclaim_margin =
    N_UP(
        DIRECTIONAL_RECLAIM_MARGIN(S0.close,level,direction)/S0.atr14,
        0.05,
        0.40
    )

wick_quality =
    N_UP(
        DIRECTIONAL_WICK(S0,direction)/BODY(S0),
        1.50,
        4.00
    )

volume_quality =
    N_UP(S0.volume_ratio, 1.20, 2.50)

setup_points =
    6.25 × (
        sweep_depth
      + reclaim_margin
      + wick_quality
      + volume_quality
    )
```

#### Continuation Setup

All inputs are accepted `15M` compression values and `S0`.

```text
compression_quality =
    N_DOWN(compression_width/S0.atr14, 0.75, 1.50)

breakout_quality =
    N_UP(
        DIRECTIONAL_BREAK_MARGIN(
            S0.close,
            COMPRESSION_BOUNDARY,
            direction
        ) / S0.atr14,
        0.05,
        0.40
    )

body_quality =
    N_UP(BODY(S0)/S0.atr14, 0.40, 1.00)

volume_quality =
    N_UP(S0.volume_ratio, 1.20, 2.50)

setup_points =
    6.25 × (
        compression_quality
      + breakout_quality
      + body_quality
      + volume_quality
    )
```

### `5M` entry-readiness quality — 20

All inputs are current `5M` `E0`, `E1`, and stored
`ENTRY_TRIGGER_PRICE`.

```text
trigger_margin =
    N_UP(
        DIRECTIONAL_BREAK_MARGIN(
            E0.close,
            ENTRY_TRIGGER_PRICE,
            direction
        ) / E0.atr14,
        0,
        0.30
    )

close_quality =
    N_UP(
        DIRECTIONAL_CLOSE_POSITION(E0,direction),
        0.65,
        0.90
    )

volume_quality =
    N_UP(E0.volume_ratio, 1.10, 2.00)

histogram_quality =
    N_UP(
        DIRECTIONAL_HISTOGRAM(E0.macd_histogram,direction)/E0.atr14,
        0,
        0.10
    )

extension_quality =
    N_DOWN(
        abs(
            DIRECTIONAL_EMA_EXTENSION(
                E0.close,
                E0.ema20,
                direction
            )
        ) / E0.atr14,
        0.25,
        0.75
    )

entry_points =
    7 × trigger_margin
  + 4 × close_quality
  + 4 × volume_quality
  + 3 × histogram_quality
  + 2 × extension_quality
```

The extension-control input is the absolute magnitude of the exact directional
EMA extension. Entry readiness still requires the direction-specific shared `5M`
EMA-side and maximum-extension conditions; scoring does not replace those gates.

### Momentum quality — 15

All `S` inputs are `15M`; all `E` inputs are `5M`.

```text
rsi_target =
    60 when direction == LONG
    40 when direction == SHORT

rsi_quality =
    N_TARGET(S0.rsi14,rsi_target,15)

histogram_15m =
    N_UP(
        DIRECTIONAL_HISTOGRAM(S0.macd_histogram,direction)/S0.atr14,
        0,
        0.10
    )

histogram_improvement =
    N_UP(
        DIRECTIONAL_DELTA(
            S0.macd_histogram,
            S1.macd_histogram,
            direction
        ) / S0.atr14,
        0,
        0.10
    )

histogram_5m =
    N_UP(
        DIRECTIONAL_HISTOGRAM(E0.macd_histogram,direction)/E0.atr14,
        0,
        0.10
    )

momentum_points =
    4.5 × rsi_quality
  + 4.5 × histogram_15m
  + 3.0 × histogram_improvement
  + 3.0 × histogram_5m
```

### Volume quality — 10

```text
volume_points =
    4 × N_UP(
        S0.volume_ratio,
        setup_minimum_15m_volume_ratio,
        2.50
    )
  + 6 × N_UP(E0.volume_ratio, 1.10, 2.50)
```

`setup_minimum_15m_volume_ratio` is selected only from the exact setup-ID table
in the helper contract.

### Universe liquidity quality — 5

Inputs are from the current accepted Universe candidate.

```text
quote_volume_quality =
    clamp01(log10(quote_volume / 10,000,000) / 2)

spread_quality =
    clamp01((10 - spread_bps) / 10)

liquidity_points =
    3 × quote_volume_quality
  + 2 × spread_quality
```

Universe mandatory acceptance guarantees `quote_volume >= 10,000,000`,
`quote_volume > 0`, and `0 <= spread_bps <= 10`. Otherwise liquidity scoring is
unavailable and Universe eligibility fails.

### Freshness margin — 5

```text
candle_age(TF) =
    max(0, run_started_at - latest_closed_candle(TF).close_time)

freshness_ratio(TF) =
    clamp01(1 - candle_age(TF) / maximum_allowed_age(TF))

freshness_points =
    5 × average(
        freshness_ratio(1H),
        freshness_ratio(15M),
        freshness_ratio(5M)
    )
```

Maximum ages are fixed positive constants in the Freshness contract. A stale flag
or age beyond the maximum fails the mandatory freshness gate before scoring.

### Final score, cap, and grade

```text
raw_score =
    clamp(
        trend_points
      + setup_points
      + entry_points
      + momentum_points
      + volume_points
      + liquidity_points
      + freshness_points,
        0,
        100
    )

rounded_score = ROUND_HALF_UP(raw_score)
```

- Entry not ready caps effective score at `84`.
- Confidence `60–69` caps effective score at `84`.
- Mandatory failure produces no score.
- Effective score maps to the approved grade bands unchanged.

## Confidence Contract

Confidence measures data reliability, rule margin, and cross-timeframe agreement;
score measures opportunity quality.

```text
data_completeness =
    average(
        min(H_series.candle_count/250,1),
        min(S_series.candle_count/250,1),
        min(E_series.candle_count/250,1)
    )

freshness_margin =
    average(
        freshness_ratio(1H),
        freshness_ratio(15M),
        freshness_ratio(5M)
    )

rule_distance_margin =
    0.60 × (setup_points/25)
  + 0.40 × (entry_points/20)

liquidity_reliability =
    liquidity_points / 5
```

Exact directional votes:

```text
vote_1H_ema_stack =
    1 when direction == LONG
        AND H0.close > H0.ema20 > H0.ema50 > H0.ema200
    1 when direction == SHORT
        AND H0.close < H0.ema20 < H0.ema50 < H0.ema200
    0 otherwise

vote_1H_structure =
    1 when direction == LONG AND H0.structure.state == bullish
    1 when direction == SHORT AND H0.structure.state == bearish
    0 otherwise

vote_1H_macd =
    1 when direction == LONG
        AND H0.macd > H0.macd_signal
        AND H0.macd_histogram > 0
    1 when direction == SHORT
        AND H0.macd < H0.macd_signal
        AND H0.macd_histogram < 0
    0 otherwise

vote_15M_close_ema20 =
    1 when direction == LONG AND S0.close > S0.ema20
    1 when direction == SHORT AND S0.close < S0.ema20
    0 otherwise

vote_15M_histogram =
    1 when DIRECTIONAL_HISTOGRAM(S0.macd_histogram,direction) > 0
    0 otherwise

vote_5M_histogram =
    1 when DIRECTIONAL_HISTOGRAM(E0.macd_histogram,direction) > 0
    0 otherwise

aligned_directional_votes =
    vote_1H_ema_stack
  + vote_1H_structure
  + vote_1H_macd
  + vote_15M_close_ema20
  + vote_15M_histogram
  + vote_5M_histogram

cross_timeframe_agreement =
    aligned_directional_votes / 6
```

```text
raw_confidence =
    25 × data_completeness
  + 20 × freshness_margin
  + 25 × rule_distance_margin
  + 20 × cross_timeframe_agreement
  + 10 × liquidity_reliability

confidence =
    ROUND_HALF_UP(clamp(raw_confidence,0,100))
```

- Confidence `>=70` may qualify when all other requirements pass.
- Confidence `60–69` is `WATCH_NEAR`.
- Confidence below `60` is Reject.
- Any unavailable confidence input makes confidence unavailable; no guessed value,
  zero substitution, or factor reweighting is allowed.

## Candidate Identity, Deduplication, and Selection

Canonical identity:

```text
UPPERCASE(symbol)|direction|setup_id|15m|reference_candle_close_time_utc_rfc3339
```

`candidate_id` is the lowercase hexadecimal SHA-256 digest of that string.

Reference candle:

- Trend Pullback: confirmation `S0`.
- Breakout Retest: breakout `B`.
- EMA Rejection: rejection `S0`.
- Liquidity Sweep Reversal: sweep `S0`.
- Continuation Setup: breakout `S0`.

Duplicate behavior:

- Same identity key updates the existing active candidate evaluation metadata.
- It does not reject or create a second candidate.
- Breakout Retest with the same `B` remains the same candidate.

Final selection:

- At most one candidate per symbol and direction.
- At most 50 non-rejected selected candidates.
- Ordering:
  1. `QUALIFIED` before `WATCH_NEAR`
  2. effective score descending
  3. confidence descending
  4. Universe rank ascending
  5. spread ascending
  6. quote volume descending
  7. symbol ascending
  8. setup ID ascending
  9. reference timestamp ascending
- Lower-ranked valid setup evaluations remain audit evidence and are excluded from
  the selected list.

## Candidate Lifecycle Contract

States:

```text
DETECTED
WATCH_NEAR
QUALIFIED
REJECTED
INVALIDATED
EXPIRED
```

`REJECTED`, `INVALIDATED`, and `EXPIRED` are terminal for the same candidate key.
Terminal keys never reactivate.

### Initial transitions

```text
NONE -> DETECTED
when valid direction and complete 15M setup are discovered.

DETECTED -> QUALIFIED
when entry ready, effective score >=85, confidence >=70, grade A or A+.

DETECTED -> WATCH_NEAR
when setup gates pass and entry is not ready,
or effective score is 80–84,
or confidence is 60–69.

DETECTED -> REJECTED
when valid score <80 or confidence <60.

DETECTED -> INVALIDATED
when deterministic trend, price, or setup invalidation occurs.

DETECTED -> EXPIRED
when candidate expiry is reached.
```

### Active refresh transitions

```text
WATCH_NEAR -> QUALIFIED
when same candidate becomes entry ready before expiry,
effective score >=85, confidence >=70, grade A or A+.

WATCH_NEAR -> WATCH_NEAR
while setup remains valid but qualification requirements do not all pass.

WATCH_NEAR -> REJECTED
when a new complete valid evaluation produces score <80 or confidence <60.

WATCH_NEAR -> INVALIDATED
when deterministic trend, price, or setup invalidation occurs.

WATCH_NEAR -> EXPIRED
when candidate expiry is reached.
```

A technical refresh failure does not invent score/confidence or force a transition.
Timestamp expiry still applies.

### QUALIFIED behavior

- A later `5M` candle failing the original entry trigger does not downgrade
  `QUALIFIED`.
- The original trigger is a qualification event, not a continuous condition.
- Post-qualification score/confidence are audit metadata only.
- `QUALIFIED` remains until:
  - fixed 15-minute qualification expiry, or
  - deterministic trend, price, or setup invalidation.
- Stale or technical refresh failure cannot newly qualify or downgrade; it records
  failure and preserves state until valid invalidation evidence or expiry.

```text
QUALIFIED -> INVALIDATED
when deterministic price/setup/trend invalidation passes.

QUALIFIED -> EXPIRED
when qualification_expires_at is reached.
```

### Re-entry and restart

- New candidate requires a new reference candle and new key.
- After terminal state, the same symbol/direction/setup has a cooldown of three newly
  closed `15M` candles, or 45 minutes.
- Breakout Retest also requires a new breakout `B`.
- Scanner V1 lifecycle authority is process memory only.
- Restart clears active and terminal in-memory records.
- Restart leaves Scanner `OFF`.
- No scan or refresh resumes automatically.
- Next explicit `START` or `RUN NOW` rebuilds from current valid closed data.

## Freshness, Time Authority, and Expiry

### Time authority

- Capture Binance exchange time once at run start as immutable `run_started_at`.
- Local UTC/exchange time absolute difference must not exceed 5 seconds.
- Exchange time unavailable rejects the run.
- `run_completed_at` is audit metadata only.
- Candle timestamp more than 2 seconds after `run_started_at` is invalid.

### Freshness thresholds

| Input | Maximum age |
|---|---:|
| Universe snapshot | 60 seconds |
| Latest `1H` closed candle | 4,500 seconds / 75 minutes |
| Latest `15M` closed candle | 1,350 seconds / 22 minutes 30 seconds |
| Latest `5M` closed candle | 450 seconds / 7 minutes 30 seconds |

- `series.stale == true` always fails freshness.
- Each series requires at least 200 contiguous, increasing, unique closed candles.

### Candidate expiry

| Setup | Expiry |
|---|---:|
| Trend Pullback | reference close + 60 minutes |
| Breakout Retest | breakout reference close + 120 minutes |
| EMA Rejection | reference close + 45 minutes |
| Liquidity Sweep Reversal | reference close + 45 minutes |
| Continuation Setup | reference close + 45 minutes |

Qualification expiry:

```text
qualification_expires_at =
    qualifying_5m_candle.close_time + 15 minutes
```

At that timestamp state becomes `EXPIRED` unless invalidation occurs first.

## OHLCV and Indicator Integrity Contract

For all required candles:

- Numeric OHLCV values must be finite.
- `open`, `high`, `low`, `close` must be greater than zero.
- `high >= max(open,close)`.
- `low <= min(open,close)`.
- `high >= low`.
- `volume >= 0`.
- `quote_volume >= 0`.
- `trades >= 0`.
- `open_time < close_time`.
- `closed == true`.
- Timestamps strictly increase and are unique.
- Adjacent periods match the declared timeframe without gaps.

Every indicator value used by a formula must be non-null and finite. Latest ATR14
and volume SMA20 must be greater than zero. Market structure must not be
`insufficient_data`.

## Failure Behavior

### Missing candles/history

- Missing timeframe data rejects that symbol with a timeframe-specific code.
- Fewer than 200 closed candles rejects that symbol.
- No shorter timeframe, stale cache, estimate, or previous output may substitute.

### Indicator failure

- Affected symbol fails closed.
- No partial or copied indicator qualifies.
- Other symbols may continue.
- Error details are sanitized.

### Rate limit

- Existing bounded retry and valid `Retry-After` handling applies.
- After exhaustion, stale cache cannot qualify.
- Symbol-specific exhaustion fails that symbol.
- Universe or exchange-time exhaustion fails the Full Universe Scan.
- No prior output is replayed as current.

### Partial/full failure

- Successful symbols continue.
- Failed symbol evaluations retain audit evidence.
- At least one success and one failure → `DEGRADED`.
- Zero successful eligible symbols → `FAILED`.
- Exchange time unavailable or Universe unavailable → `FAILED`.
- Failed full scan creates no new candidates.

### Active refresh technical failure

- Cannot qualify a candidate.
- Records failure and preserves prior active state.
- Timestamp expiry still applies.
- Later valid refresh may transition according to lifecycle rules.

### Overlap/restart

- Concurrent full scan or refresh is skipped with `SCAN_ALREADY_RUNNING`.
- No queue or overlapping mutation.
- Restart clears process memory and leaves Scanner `OFF`.
- No synthetic lifecycle event is emitted during startup.

## Rejection Taxonomy

Duplicate and superseded-selection conditions are not rejection codes.

### Run/data rejection codes

| Code | Trigger |
|---|---|
| `MARKET_TIME_UNAVAILABLE` | Binance exchange time unavailable. |
| `CLOCK_SKEW_EXCEEDED` | Clock difference exceeds 5 seconds. |
| `UNIVERSE_UNAVAILABLE` | Universe cannot be built. |
| `UNIVERSE_STALE` | Universe age exceeds 60 seconds. |
| `RATE_LIMIT_EXHAUSTED` | Required bounded 429 retries exhausted. |
| `FULL_MARKET_DATA_FAILURE` | No eligible symbol completes evaluation. |
| `MISSING_1H_CANDLES` | Required `1H` series unavailable/empty. |
| `MISSING_15M_CANDLES` | Required `15M` series unavailable/empty. |
| `MISSING_5M_CANDLES` | Required `5M` series unavailable/empty. |
| `INSUFFICIENT_1H_HISTORY` | Fewer than 200 closed `1H` candles. |
| `INSUFFICIENT_15M_HISTORY` | Fewer than 200 closed `15M` candles. |
| `INSUFFICIENT_5M_HISTORY` | Fewer than 200 closed `5M` candles. |
| `STALE_1H_DATA` | Stale flag or age >4,500 seconds. |
| `STALE_15M_DATA` | Stale flag or age >1,350 seconds. |
| `STALE_5M_DATA` | Stale flag or age >450 seconds. |
| `INVALID_1H_OHLCV` | `1H` integrity/continuity failure. |
| `INVALID_15M_OHLCV` | `15M` integrity/continuity failure. |
| `INVALID_5M_OHLCV` | `5M` integrity/continuity failure. |
| `MISSING_REQUIRED_INDICATOR` | Required indicator unavailable/non-finite. |
| `INDICATOR_CALCULATION_FAILED` | Indicator calculation/input failure. |
| `STRUCTURE_INSUFFICIENT` | Structure is `insufficient_data`. |
| `UNIVERSE_ELIGIBILITY_FAILED` | Symbol absent from current eligible Universe. |

### Regime/setup rejection codes

| Code | Trigger |
|---|---|
| `TREND_SIDEWAYS` | Exact `SIDEWAYS` formula true. |
| `TREND_MIXED` | Neither direction passes valid regime data. |
| `TREND_DIRECTION_MISMATCH` | Candidate conflicts with regime direction. |
| `VOLATILITY_BELOW_MINIMUM` | Normalized ATR below minimum. |
| `VOLATILITY_ABOVE_MAXIMUM` | Normalized ATR above maximum. |
| `PULLBACK_SEQUENCE_FAILED` | Pullback sequence fails. |
| `PULLBACK_ZONE_MISSED` | Pullback EMA-zone rule fails. |
| `BREAKOUT_NOT_CONFIRMED` | No valid breakout `B`. |
| `RETEST_NOT_CONFIRMED` | Retest proximity/hold fails. |
| `EMA_REJECTION_NOT_CONFIRMED` | EMA rejection formula fails. |
| `LIQUIDITY_SWEEP_NOT_CONFIRMED` | Sweep formula fails. |
| `CONTINUATION_COMPRESSION_FAILED` | Compression formula fails. |
| `CONTINUATION_BREAKOUT_FAILED` | Continuation breakout fails. |
| `VOLUME_BELOW_MINIMUM` | Required volume ratio fails. |
| `STRUCTURE_CONDITION_FAILED` | Required prior level/structure fails. |
| `SETUP_INVALIDATED` | Setup invalidation formula true. |
| `SETUP_NOT_DETECTED` | No setup passes after data/regime gates. |
| `SCORE_BELOW_80` | Complete valid score below 80. |
| `CONFIDENCE_BELOW_60` | Complete valid confidence below 60. |
| `REENTRY_COOLDOWN_ACTIVE` | Three new `15M` candles not completed. |

## Watch/Near Reason Taxonomy

| Code | Trigger |
|---|---|
| `ENTRY_NOT_READY` | Valid setup but exact `5M` entry false. |
| `ENTRY_OVEREXTENDED` | Directional EMA extension >`0.75 × ATR5`. |
| `GRADE_B_PLUS_WATCH_ONLY` | Effective score 80–84. |
| `CONFIDENCE_WATCH_ONLY` | Confidence 60–69. |

## Invalidation and Expiry Taxonomy

| Code | Trigger |
|---|---|
| `CANDIDATE_INVALIDATED` | Deterministic trend/price/setup invalidation. |
| `CANDIDATE_EXPIRED` | Candidate or qualification expiry reached. |

## Audit and Selection Taxonomy

These codes never reject a valid candidate:

| Code | Behavior |
|---|---|
| `SCAN_ALREADY_RUNNING` | Skip overlap; active run continues. |
| `DUPLICATE_CANDIDATE_UPDATED` | Update same-key active candidate; do not reject/duplicate. |
| `SUPERSEDED_BY_HIGHER_RANKED_SETUP` | Retain audit evidence; exclude from selected list. |
| `PARTIAL_SYMBOL_FAILURE` | Mark run `DEGRADED`; successful symbols continue. |

Every record includes when applicable:

- code
- Scanner run ID
- symbol
- direction
- setup ID
- timeframe
- candidate/reference timestamp
- observed value
- required threshold
- sanitized detail

## Implementation Acceptance Gate

Scanner Contract Version 1 is approved. Scanner runtime remains unimplemented and
blocked until:

1. This exact contract revision is merged into `main`.
2. README and this canonical file remain consistent on `main`.
3. Owner approves a separate Scanner runtime task.
4. Runtime work includes deterministic unit, contract, API integration,
   cadence/lock, lifecycle, failure, and audit tests.
5. Ruff, strict Mypy, Pytest with coverage, CI, and changed-files review pass.

Until then:

- Do not add Scanner schemas, services, routes, schedulers, or candidate logic.
- Do not expose a Scanner endpoint.
- Do not emit LONG/SHORT runtime candidates.
- Do not calculate runtime score, confidence, grade, or lifecycle output.
- Do not access account data or add execution behavior.
