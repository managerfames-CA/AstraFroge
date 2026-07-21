"""Phase 4 final Signal Decision Engine.

The service consumes deterministic Scanner/Strategy facts only. It does not fetch market
or account data, calculate indicators, or perform execution side effects.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation

from app.schemas.scanner import CandidateLifecycle, ScannerCandidate, ScannerGrade
from app.schemas.signal_decision import (
    EntryTriggerStatus,
    SignalDecision,
    SignalDecisionStatus,
)
from app.services.scanner_contract import QUALIFICATION_EXPIRY

_MISSING_PROVENANCE = "MISSING_SOURCE_PROVENANCE"
_SOURCE_STALE = "SOURCE_SNAPSHOT_STALE"
_DECISION_EXPIRED = "SIGNAL_DECISION_EXPIRED"
_CONFLICTING_FACTS = "CONFLICTING_STRATEGY_FIELDS"
_TRIGGER_INVALID = "ENTRY_TRIGGER_INVALID"

_WATCH_CODES = frozenset(
    {
        "ENTRY_NOT_READY",
        "ENTRY_OVEREXTENDED",
        "GRADE_B_PLUS_WATCH_ONLY",
        "CONFIDENCE_WATCH_ONLY",
        "VOLUME_BELOW_MINIMUM",
    }
)
_BLOCKING_CODES = frozenset(
    {
        "CONFIDENCE_BELOW_60",
        "SCORE_BELOW_80",
        "CANDIDATE_INVALIDATED",
        "CANDIDATE_EXPIRED",
        "SETUP_INVALIDATED",
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
        "TREND_SIDEWAYS",
        "TREND_MIXED",
        "TREND_DIRECTION_MISMATCH",
    }
)


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _optional_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class SignalDecisionEngine:
    """Own final READY/NEAR_SETUP/REJECTED eligibility and stable identity."""

    def decide(self, candidate: ScannerCandidate) -> SignalDecision:
        """Convert one deterministic candidate fact set into one final decision."""

        rejection_reasons: list[str] = []
        watch_reasons: list[str] = []
        strategy_reasons = self._strategy_reasons(candidate)
        source_version = candidate.evidence.get("source_snapshot_version")
        if not isinstance(source_version, str) or not source_version.strip():
            source_version = None
            _append_unique(rejection_reasons, _MISSING_PROVENANCE)

        if candidate.stale:
            _append_unique(rejection_reasons, _SOURCE_STALE)
        if candidate.evaluated_at >= candidate.expires_at:
            _append_unique(rejection_reasons, _DECISION_EXPIRED)
        if self._facts_conflict(candidate):
            _append_unique(rejection_reasons, _CONFLICTING_FACTS)

        if candidate.lifecycle is CandidateLifecycle.INVALIDATED:
            _append_unique(rejection_reasons, "CANDIDATE_INVALIDATED")
        elif candidate.lifecycle is CandidateLifecycle.EXPIRED:
            _append_unique(rejection_reasons, "CANDIDATE_EXPIRED")
        elif candidate.lifecycle is CandidateLifecycle.REJECTED:
            _append_unique(rejection_reasons, "SETUP_INVALIDATED")

        if candidate.grade is ScannerGrade.REJECT:
            _append_unique(rejection_reasons, "SCORE_BELOW_80")
        if candidate.score is not None and candidate.score < 80:
            _append_unique(rejection_reasons, "SCORE_BELOW_80")
        if candidate.confidence is not None and candidate.confidence < 60:
            _append_unique(rejection_reasons, "CONFIDENCE_BELOW_60")

        for code in candidate.audit_codes:
            if code in _WATCH_CODES:
                _append_unique(watch_reasons, code)
            elif code in _BLOCKING_CODES:
                _append_unique(rejection_reasons, code)
            else:
                _append_unique(strategy_reasons, code)

        trigger_status = self._trigger_status(candidate)
        if trigger_status is EntryTriggerStatus.INVALID:
            _append_unique(rejection_reasons, _TRIGGER_INVALID)

        fresh = not rejection_reasons and not candidate.stale
        grade_ready = candidate.grade in {ScannerGrade.A_PLUS, ScannerGrade.A}
        numeric_ready = bool(
            candidate.score is not None
            and candidate.score >= 85
            and candidate.confidence is not None
            and candidate.confidence >= 70
        )
        ready = bool(
            fresh
            and grade_ready
            and numeric_ready
            and trigger_status is EntryTriggerStatus.READY
        )

        if rejection_reasons:
            status = SignalDecisionStatus.REJECTED
        elif ready:
            status = SignalDecisionStatus.READY
        else:
            status = SignalDecisionStatus.NEAR_SETUP
            if trigger_status is EntryTriggerStatus.PENDING:
                _append_unique(watch_reasons, "ENTRY_NOT_READY")
            if candidate.grade is ScannerGrade.B_PLUS:
                _append_unique(watch_reasons, "GRADE_B_PLUS_WATCH_ONLY")
            if candidate.confidence is not None and 60 <= candidate.confidence <= 69:
                _append_unique(watch_reasons, "CONFIDENCE_WATCH_ONLY")

        expires_at = self._decision_expiry(candidate, status)
        decision_key = self._decision_key(candidate, source_version)
        return SignalDecision(
            decision_key=decision_key,
            symbol=candidate.symbol,
            direction=candidate.direction,
            setup=candidate.setup,
            setup_name=candidate.setup_name,
            decision_status=status,
            grade=candidate.grade,
            score=candidate.score,
            confidence=candidate.confidence,
            risk_reward=_optional_decimal(candidate.evidence.get("risk_reward")),
            entry_trigger_status=trigger_status,
            selected=ready,
            ready=ready,
            rejection_reasons=rejection_reasons,
            watch_reasons=watch_reasons,
            strategy_reasons=strategy_reasons,
            source_snapshot_version=source_version,
            evaluated_at=candidate.evaluated_at,
            expires_at=expires_at,
            fresh=fresh,
        )

    @staticmethod
    def _facts_conflict(candidate: ScannerCandidate) -> bool:
        if candidate.score is None or candidate.confidence is None or candidate.grade is None:
            return True
        if candidate.reference_close_time > candidate.setup_confirmed_at:
            return True
        if candidate.setup_confirmed_at > candidate.evaluated_at:
            return True
        if candidate.expires_at <= candidate.setup_confirmed_at:
            return True
        if candidate.entry_trigger_price <= 0:
            return True
        if candidate.grade is ScannerGrade.B_PLUS and candidate.score < 80:
            return True
        if candidate.grade in {ScannerGrade.A_PLUS, ScannerGrade.A} and candidate.score < 85:
            return True
        return False

    @staticmethod
    def _trigger_status(candidate: ScannerCandidate) -> EntryTriggerStatus:
        if candidate.entry_trigger_price <= 0:
            return EntryTriggerStatus.INVALID
        return EntryTriggerStatus.READY if candidate.entry_ready else EntryTriggerStatus.PENDING

    @staticmethod
    def _strategy_reasons(candidate: ScannerCandidate) -> list[str]:
        raw = candidate.evidence.get("strategy_reason_codes", [])
        if not isinstance(raw, list):
            return []
        return list(dict.fromkeys(item for item in raw if isinstance(item, str) and item))

    @staticmethod
    def _decision_expiry(
        candidate: ScannerCandidate,
        status: SignalDecisionStatus,
    ) -> datetime:
        if status is not SignalDecisionStatus.READY:
            return candidate.expires_at
        entry_close = _optional_datetime(candidate.evidence.get("entry_snapshot_close_time"))
        if entry_close is None:
            return candidate.expires_at
        return min(candidate.expires_at, entry_close + QUALIFICATION_EXPIRY)

    @staticmethod
    def _decision_key(candidate: ScannerCandidate, source_version: str | None) -> str:
        identity = "|".join(
            (
                "astraforge-signal-decision-v1",
                candidate.symbol.upper(),
                candidate.direction.value,
                candidate.setup.value,
                source_version or "missing-provenance",
                candidate.reference_close_time.isoformat(),
                format(candidate.entry_trigger_price, "f"),
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()
