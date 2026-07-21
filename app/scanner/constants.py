"""Centralized deterministic Scanner Contract Version 1 constants."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

CONTRACT_VERSION = "1"
SCANNER_MAX_SYMBOLS = 20
SCANNER_PREFILTER_POOL_SYMBOLS = 120
SCANNER_RUN_HISTORY_LIMIT = 100
SCANNER_TERMINAL_CANDIDATE_LIMIT = 500
SCANNER_TERMINAL_HISTORY_LIMIT = 1000
MIN_CLOSED_CANDLES = 200
CONFIDENCE_COMPLETENESS_FULL_COUNT = 250

FULL_SCAN_INTERVAL = timedelta(minutes=15)
ACTIVE_REFRESH_INTERVAL = timedelta(minutes=5)
UNIVERSE_REFRESH_INTERVAL = timedelta(minutes=30)
REENTRY_COOLDOWN = timedelta(minutes=45)
QUALIFICATION_EXPIRY = timedelta(minutes=15)
MAX_CLOCK_SKEW = timedelta(seconds=5)
FUTURE_CANDLE_TOLERANCE = timedelta(seconds=2)
UNIVERSE_MAX_AGE = timedelta(seconds=60)
TIMEFRAME_MAX_AGE = {
    "1h": timedelta(minutes=75),
    "15m": timedelta(minutes=22, seconds=30),
    "5m": timedelta(minutes=7, seconds=30),
}
TIMEFRAME_INTERVAL = {
    "1h": timedelta(hours=1),
    "15m": timedelta(minutes=15),
    "5m": timedelta(minutes=5),
}

SETUP_IDS = (
    "trend_pullback",
    "breakout_retest",
    "ema_rejection",
    "liquidity_sweep_reversal",
    "continuation_setup",
)
SETUP_NAMES = {
    "trend_pullback": "Trend Pullback",
    "breakout_retest": "Breakout Retest",
    "ema_rejection": "EMA Rejection",
    "liquidity_sweep_reversal": "Liquidity Sweep Reversal",
    "continuation_setup": "Continuation Setup",
}
SETUP_EXPIRY = {
    "trend_pullback": timedelta(minutes=60),
    "breakout_retest": timedelta(minutes=120),
    "ema_rejection": timedelta(minutes=45),
    "liquidity_sweep_reversal": timedelta(minutes=45),
    "continuation_setup": timedelta(minutes=45),
}
SETUP_MIN_VOLUME_RATIO = {
    "trend_pullback": Decimal("0.80"),
    "breakout_retest": Decimal("0.80"),
    "ema_rejection": Decimal("1.00"),
    "liquidity_sweep_reversal": Decimal("1.20"),
    "continuation_setup": Decimal("1.20"),
}

GRADE_A_PLUS_MIN = 90
GRADE_A_MIN = 85
GRADE_B_PLUS_MIN = 80
ENTRY_NOT_READY_SCORE_CAP = 84
CONFIDENCE_WATCH_SCORE_CAP = 84
CONFIDENCE_QUALIFY_MIN = 70
CONFIDENCE_WATCH_MIN = 60

SCORE_WEIGHTS = {
    "trend": Decimal("20"),
    "setup": Decimal("25"),
    "entry": Decimal("20"),
    "momentum": Decimal("15"),
    "volume": Decimal("10"),
    "liquidity": Decimal("5"),
    "freshness": Decimal("5"),
}
CONFIDENCE_WEIGHTS = {
    "data_completeness": Decimal("25"),
    "freshness": Decimal("20"),
    "rule_distance": Decimal("25"),
    "cross_timeframe": Decimal("20"),
    "liquidity": Decimal("10"),
}

VOLATILITY_LIMITS = {
    "15m": (Decimal("0.0015"), Decimal("0.025")),
    "5m": (Decimal("0.0005"), Decimal("0.015")),
}

REJECTION_CODES = frozenset(
    {
        "MARKET_TIME_UNAVAILABLE",
        "CLOCK_SKEW_EXCEEDED",
        "UNIVERSE_UNAVAILABLE",
        "UNIVERSE_STALE",
        "RATE_LIMIT_EXHAUSTED",
        "FULL_MARKET_DATA_FAILURE",
        "MISSING_1H_CANDLES",
        "MISSING_15M_CANDLES",
        "MISSING_5M_CANDLES",
        "INSUFFICIENT_1H_HISTORY",
        "INSUFFICIENT_15M_HISTORY",
        "INSUFFICIENT_5M_HISTORY",
        "STALE_1H_DATA",
        "STALE_15M_DATA",
        "STALE_5M_DATA",
        "INVALID_1H_OHLCV",
        "INVALID_15M_OHLCV",
        "INVALID_5M_OHLCV",
        "MISSING_REQUIRED_INDICATOR",
        "INDICATOR_CALCULATION_FAILED",
        "STRUCTURE_INSUFFICIENT",
        "UNIVERSE_ELIGIBILITY_FAILED",
        "TREND_SIDEWAYS",
        "TREND_MIXED",
        "TREND_DIRECTION_MISMATCH",
        "VOLATILITY_BELOW_MINIMUM",
        "VOLATILITY_ABOVE_MAXIMUM",
        "PULLBACK_SEQUENCE_FAILED",
        "PULLBACK_ZONE_MISSED",
        "BREAKOUT_NOT_CONFIRMED",
        "RETEST_NOT_CONFIRMED",
        "EMA_REJECTION_NOT_CONFIRMED",
        "LIQUIDITY_SWEEP_NOT_CONFIRMED",
        "CONTINUATION_COMPRESSION_FAILED",
        "CONTINUATION_BREAKOUT_FAILED",
        "VOLUME_BELOW_MINIMUM",
        "STRUCTURE_CONDITION_FAILED",
        "SETUP_INVALIDATED",
        "SETUP_NOT_DETECTED",
        "SCORE_BELOW_80",
        "CONFIDENCE_BELOW_60",
        "REENTRY_COOLDOWN_ACTIVE",
    }
)
WATCH_REASON_CODES = frozenset(
    {
        "ENTRY_NOT_READY",
        "ENTRY_OVEREXTENDED",
        "GRADE_B_PLUS_WATCH_ONLY",
        "CONFIDENCE_WATCH_ONLY",
    }
)
INVALIDATION_CODES = frozenset({"CANDIDATE_INVALIDATED", "CANDIDATE_EXPIRED"})
AUDIT_CODES = frozenset(
    {
        "SCAN_ALREADY_RUNNING",
        "DUPLICATE_CANDIDATE_UPDATED",
        "SUPERSEDED_BY_HIGHER_RANKED_SETUP",
        "PARTIAL_SYMBOL_FAILURE",
    }
)
