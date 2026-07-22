"""Versioned process-scoped Signal Engine sourced from Scanner runtime output."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from app.schemas.scanner import (
    CandidateLifecycle,
    ScannerCandidate,
    ScannerGrade,
    ScannerRunStatus,
    ScannerRunSummary,
    ScannerState,
)
from app.schemas.signals import (
    SignalEngineState,
    SignalLifecycle,
    SignalRecord,
    SignalRecordList,
    SignalStatusResponse,
    SignalSummary,
    SignalTransition,
)
from app.services.scanner import ScannerService

SIGNAL_RECORD_LIMIT = 1_000
_TERMINAL_LIFECYCLES = frozenset(
    {
        SignalLifecycle.EXPIRED,
        SignalLifecycle.INVALIDATED,
        SignalLifecycle.REJECTED,
        SignalLifecycle.RISK_BLOCKED,
    }
)


class SignalService:
    """Maintain stable, versioned Signal records independently from Scanner views."""

    def __init__(
        self,
        scanner_service: ScannerService,
        *,
        record_limit: int = SIGNAL_RECORD_LIMIT,
    ) -> None:
        if record_limit < 1:
            raise ValueError("Signal record limit must be positive")
        self._scanner = scanner_service
        self._record_limit = record_limit
        self._records_by_candidate: dict[str, SignalRecord] = {}
        self._candidate_by_signal: dict[str, str] = {}

    def status(self) -> SignalStatusResponse:
        scanner_status = self._scanner.status()
        latest_run = scanner_status.latest_run
        self._synchronize(self._source_invalidation_time(latest_run))
        signals = self._ordered_records()
        active = sum(signal.lifecycle is SignalLifecycle.ACTIVE for signal in signals)
        watch = sum(signal.lifecycle is SignalLifecycle.WATCH for signal in signals)
        terminal = len(signals) - active - watch
        latest_scanner_run_at = (
            latest_run.completed_at if latest_run is not None else None
        )
        updated_at = max(
            (signal.updated_at or signal.evaluated_at for signal in signals),
            default=latest_scanner_run_at,
        )
        engine_state = (
            SignalEngineState.READY
            if scanner_status.state is ScannerState.ON or signals
            else SignalEngineState.WAITING_FOR_SCANNER
        )
        summary = SignalSummary(
            active_signals=sum(
                signal.lifecycle is SignalLifecycle.ACTIVE for signal in signals
            ),
            a_plus_signals=sum(
                signal.lifecycle is SignalLifecycle.ACTIVE
                and signal.grade is ScannerGrade.A_PLUS
                for signal in signals
            ),
            a_signals=sum(
                signal.lifecycle is SignalLifecycle.ACTIVE
                and signal.grade is ScannerGrade.A
                for signal in signals
            ),
            b_plus_watch=sum(
                signal.lifecycle is SignalLifecycle.WATCH
                and signal.grade is ScannerGrade.B_PLUS
                for signal in signals
            ),
            expired=sum(
                signal.lifecycle is SignalLifecycle.EXPIRED for signal in signals
            ),
            risk_blocked=sum(
                signal.lifecycle is SignalLifecycle.RISK_BLOCKED for signal in signals
            ),
        )
        return SignalStatusResponse(
            state=engine_state,
            scanner_state=scanner_status.state.value,
            active_signal_count=active,
            watch_signal_count=watch,
            terminal_signal_count=terminal,
            updated_at=updated_at,
            latest_scanner_run_at=latest_scanner_run_at,
            summary=summary,
        )

    def signals(self) -> SignalRecordList:
        scanner_status = self._scanner.status()
        self._synchronize(self._source_invalidation_time(scanner_status.latest_run))
        records = self._ordered_records()
        return SignalRecordList(count=len(records), signals=records)

    def get(self, signal_id: str) -> SignalRecord | None:
        """Return one stable Signal record after synchronizing current Scanner state."""

        self.signals()
        candidate_id = self._candidate_by_signal.get(signal_id)
        if candidate_id is None:
            return None
        return self._records_by_candidate.get(candidate_id)

    def mark_risk_blocked(
        self,
        signal_id: str,
        *,
        reason: str,
        changed_at: datetime | None = None,
    ) -> SignalRecord | None:
        """Terminally risk-block one active Signal while retaining its audit history."""

        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("Risk block reason is required")
        record = self.get(signal_id)
        if record is None or record.lifecycle in _TERMINAL_LIFECYCLES:
            return record
        transition_at = changed_at or record.updated_at or record.evaluated_at
        updated = self._transition(
            record,
            SignalLifecycle.RISK_BLOCKED,
            changed_at=transition_at,
            reason=normalized_reason,
        )
        self._records_by_candidate[record.candidate_id] = updated
        return updated

    def _synchronize(self, latest_scanner_run_at: datetime | None) -> None:
        seen_candidates: set[str] = set()
        for candidate in self._scanner.candidates():
            seen_candidates.add(candidate.candidate_id)
            lifecycle = self._map_lifecycle(candidate.lifecycle)
            existing = self._records_by_candidate.get(candidate.candidate_id)
            if existing is None:
                record = self._create_record(candidate, lifecycle)
                self._records_by_candidate[candidate.candidate_id] = record
                self._candidate_by_signal[record.signal_id] = candidate.candidate_id
                continue
            if (
                existing.lifecycle in _TERMINAL_LIFECYCLES
                and lifecycle is not existing.lifecycle
            ):
                continue
            self._records_by_candidate[candidate.candidate_id] = self._update_record(
                existing,
                candidate,
                lifecycle,
            )

        if latest_scanner_run_at is not None:
            for candidate_id, record in list(self._records_by_candidate.items()):
                if (
                    candidate_id in seen_candidates
                    or record.lifecycle in _TERMINAL_LIFECYCLES
                ):
                    continue
                record_time = record.updated_at or record.evaluated_at
                if latest_scanner_run_at < record_time:
                    continue
                self._records_by_candidate[candidate_id] = self._transition(
                    record,
                    SignalLifecycle.INVALIDATED,
                    changed_at=latest_scanner_run_at,
                    reason="SOURCE_CANDIDATE_MISSING",
                )

        self._prune_records()

    def _create_record(
        self,
        candidate: ScannerCandidate,
        lifecycle: SignalLifecycle,
    ) -> SignalRecord:
        signal_id = self._signal_id(candidate.candidate_id)
        transition = SignalTransition(
            sequence=1,
            lifecycle=lifecycle,
            changed_at=candidate.evaluated_at,
            reason=f"SCANNER_{candidate.lifecycle.value}",
        )
        return SignalRecord(
            signal_id=signal_id,
            candidate_id=candidate.candidate_id,
            version=1,
            lifecycle=lifecycle,
            created_at=candidate.evaluated_at,
            updated_at=candidate.evaluated_at,
            terminal_at=(
                candidate.evaluated_at if lifecycle in _TERMINAL_LIFECYCLES else None
            ),
            lifecycle_history=[transition],
            **self._candidate_values(candidate),
        )

    def _update_record(
        self,
        existing: SignalRecord,
        candidate: ScannerCandidate,
        lifecycle: SignalLifecycle,
    ) -> SignalRecord:
        values = self._candidate_values(candidate)
        values["lifecycle"] = lifecycle
        changed = any(
            getattr(existing, key) != value for key, value in values.items()
        )
        if not changed:
            return existing

        history = list(existing.lifecycle_history)
        terminal_at = existing.terminal_at
        if lifecycle is not existing.lifecycle:
            history.append(
                SignalTransition(
                    sequence=len(history) + 1,
                    lifecycle=lifecycle,
                    changed_at=candidate.evaluated_at,
                    reason=f"SCANNER_{candidate.lifecycle.value}",
                )
            )
            if lifecycle in _TERMINAL_LIFECYCLES:
                terminal_at = candidate.evaluated_at

        values.update(
            {
                "version": existing.version + 1,
                "updated_at": candidate.evaluated_at,
                "terminal_at": terminal_at,
                "lifecycle_history": history,
            }
        )
        return existing.model_copy(update=values)

    def _candidate_values(self, candidate: ScannerCandidate) -> dict[str, Any]:
        source_run_id = candidate.evidence.get("source_run_id")
        stop_provider = getattr(self._scanner, "risk_stop_price", None)
        stop_loss_price = (
            stop_provider(candidate.candidate_id) if callable(stop_provider) else None
        )
        return {
            "symbol": candidate.symbol,
            "direction": candidate.direction,
            "setup": candidate.setup,
            "setup_name": candidate.setup_name,
            "scanner_lifecycle": candidate.lifecycle,
            "grade": candidate.grade,
            "score": candidate.score,
            "confidence": candidate.confidence,
            "entry_ready": candidate.entry_ready,
            "entry_trigger_price": candidate.entry_trigger_price,
            "stop_loss_price": stop_loss_price,
            "reference_close_time": candidate.reference_close_time,
            "setup_confirmed_at": candidate.setup_confirmed_at,
            "expires_at": candidate.expires_at,
            "qualification_expires_at": candidate.qualification_expires_at,
            "evaluated_at": candidate.evaluated_at,
            "source_run_id": source_run_id if isinstance(source_run_id, str) else None,
            "universe_rank": candidate.universe_rank,
            "quote_volume": candidate.quote_volume,
            "spread_bps": candidate.spread_bps,
            "accepted_reasons": list(candidate.accepted_reasons),
            "audit_codes": list(candidate.audit_codes),
        }

    def _transition(
        self,
        record: SignalRecord,
        lifecycle: SignalLifecycle,
        *,
        changed_at: datetime,
        reason: str,
    ) -> SignalRecord:
        if record.lifecycle is lifecycle:
            return record
        history = [
            *record.lifecycle_history,
            SignalTransition(
                sequence=len(record.lifecycle_history) + 1,
                lifecycle=lifecycle,
                changed_at=changed_at,
                reason=reason,
            ),
        ]
        audit_codes = list(record.audit_codes)
        if reason not in audit_codes:
            audit_codes.append(reason)
        return record.model_copy(
            update={
                "version": record.version + 1,
                "lifecycle": lifecycle,
                "updated_at": changed_at,
                "terminal_at": (
                    changed_at if lifecycle in _TERMINAL_LIFECYCLES else None
                ),
                "audit_codes": audit_codes,
                "lifecycle_history": history,
            }
        )

    def _prune_records(self) -> None:
        overflow = len(self._records_by_candidate) - self._record_limit
        if overflow <= 0:
            return

        prune_rank = {
            SignalLifecycle.EXPIRED: 0,
            SignalLifecycle.INVALIDATED: 0,
            SignalLifecycle.REJECTED: 0,
            SignalLifecycle.RISK_BLOCKED: 0,
            SignalLifecycle.WATCH: 1,
            SignalLifecycle.ACTIVE: 2,
        }
        records = sorted(
            self._records_by_candidate.values(),
            key=lambda record: (
                prune_rank[record.lifecycle],
                *self._retention_order(record),
            ),
        )
        for record in records[:overflow]:
            self._records_by_candidate.pop(record.candidate_id, None)
            self._candidate_by_signal.pop(record.signal_id, None)

    def _ordered_records(self) -> list[SignalRecord]:
        lifecycle_rank = {
            SignalLifecycle.ACTIVE: 0,
            SignalLifecycle.WATCH: 1,
            SignalLifecycle.RISK_BLOCKED: 2,
            SignalLifecycle.INVALIDATED: 3,
            SignalLifecycle.EXPIRED: 4,
            SignalLifecycle.REJECTED: 5,
        }
        return sorted(
            self._records_by_candidate.values(),
            key=lambda signal: (
                lifecycle_rank[signal.lifecycle],
                -(signal.score if signal.score is not None else -1),
                -(signal.confidence if signal.confidence is not None else -1),
                signal.universe_rank,
                signal.symbol,
                signal.signal_id,
            ),
        )

    @staticmethod
    def _retention_order(record: SignalRecord) -> tuple[datetime, str]:
        return (
            record.terminal_at or record.updated_at or record.evaluated_at,
            record.signal_id,
        )

    @staticmethod
    def _source_invalidation_time(latest_run: ScannerRunSummary | None) -> datetime | None:
        if latest_run is None or latest_run.status is not ScannerRunStatus.COMPLETED:
            return None
        return latest_run.completed_at

    @staticmethod
    def _signal_id(candidate_id: str) -> str:
        payload = f"astraforge-signal-v1:{candidate_id}".encode()
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _map_lifecycle(lifecycle: CandidateLifecycle) -> SignalLifecycle:
        mapping = {
            CandidateLifecycle.QUALIFIED: SignalLifecycle.ACTIVE,
            CandidateLifecycle.WATCH_NEAR: SignalLifecycle.WATCH,
            CandidateLifecycle.DETECTED: SignalLifecycle.WATCH,
            CandidateLifecycle.EXPIRED: SignalLifecycle.EXPIRED,
            CandidateLifecycle.INVALIDATED: SignalLifecycle.INVALIDATED,
            CandidateLifecycle.REJECTED: SignalLifecycle.REJECTED,
        }
        return mapping[lifecycle]
