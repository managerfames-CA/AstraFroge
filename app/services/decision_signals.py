"""Decision-backed SignalService and durable PostgreSQL adapter for Phase 4."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.persistence.models import SignalRow
from app.persistence.repositories import TradingStateRepositories
from app.persistence.service_adapters import _json, _payload
from app.schemas.scanner import ScannerCandidate
from app.schemas.signal_decision import SignalDecision, SignalDecisionStatus
from app.schemas.signals import (
    SignalLifecycle,
    SignalRecord,
    SignalRecordList,
    SignalStatusResponse,
    SignalTransition,
)
from app.services.scanner import ScannerService
from app.services.signal_decision import SignalDecisionEngine
from app.services.signals import SIGNAL_RECORD_LIMIT, SignalService

_TERMINAL_LIFECYCLES = frozenset(
    {
        SignalLifecycle.EXPIRED,
        SignalLifecycle.INVALIDATED,
        SignalLifecycle.REJECTED,
        SignalLifecycle.RISK_BLOCKED,
    }
)


class DecisionBackedSignalService(SignalService):
    """Map Strategy/Scanner facts through the sole final SignalDecisionEngine authority."""

    def __init__(
        self,
        scanner_service: ScannerService,
        decision_engine: SignalDecisionEngine,
        *,
        record_limit: int = SIGNAL_RECORD_LIMIT,
    ) -> None:
        super().__init__(scanner_service, record_limit=record_limit)
        self._decision_engine = decision_engine
        self._records_by_decision: dict[str, SignalRecord] = {}
        self._decision_by_signal: dict[str, str] = {}
        # Keep inherited storage references valid for status/ordering helpers.
        self._records_by_candidate = self._records_by_decision
        self._candidate_by_signal = self._decision_by_signal

    def get(self, signal_id: str) -> SignalRecord | None:
        self.signals()
        decision_key = self._decision_by_signal.get(signal_id)
        if decision_key is None:
            return None
        return self._records_by_decision.get(decision_key)

    def mark_risk_blocked(
        self,
        signal_id: str,
        *,
        reason: str,
        changed_at: datetime | None = None,
    ) -> SignalRecord | None:
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
        assert record.decision_key is not None
        self._records_by_decision[record.decision_key] = updated
        return updated

    def _synchronize(self, latest_scanner_run_at: datetime | None) -> None:
        seen_decisions: set[str] = set()
        for candidate in self._scanner.candidates():
            decision = self._decision_engine.decide(candidate)
            decision_key = decision.decision_key
            seen_decisions.add(decision_key)
            lifecycle = self._decision_lifecycle(decision)
            existing = self._records_by_decision.get(decision_key)
            if existing is None:
                record = self._create_decision_record(candidate, decision, lifecycle)
                self._records_by_decision[decision_key] = record
                self._decision_by_signal[record.signal_id] = decision_key
                continue
            if existing.lifecycle in _TERMINAL_LIFECYCLES and lifecycle is not existing.lifecycle:
                continue
            self._records_by_decision[decision_key] = self._update_decision_record(
                existing,
                candidate,
                decision,
                lifecycle,
            )

        if latest_scanner_run_at is not None:
            for decision_key, record in list(self._records_by_decision.items()):
                if decision_key in seen_decisions or record.lifecycle in _TERMINAL_LIFECYCLES:
                    continue
                record_time = record.updated_at or record.evaluated_at
                if latest_scanner_run_at < record_time:
                    continue
                self._records_by_decision[decision_key] = self._transition(
                    record,
                    SignalLifecycle.INVALIDATED,
                    changed_at=latest_scanner_run_at,
                    reason="SOURCE_DECISION_SUPERSEDED",
                )
        self._prune_decision_records()

    def _create_decision_record(
        self,
        candidate: ScannerCandidate,
        decision: SignalDecision,
        lifecycle: SignalLifecycle,
    ) -> SignalRecord:
        signal_id = self._signal_id(decision.decision_key)
        reason = f"DECISION_{decision.decision_status.value}"
        transition = SignalTransition(
            sequence=1,
            lifecycle=lifecycle,
            changed_at=decision.evaluated_at,
            reason=reason,
        )
        return SignalRecord(
            signal_id=signal_id,
            candidate_id=candidate.candidate_id,
            decision_key=decision.decision_key,
            version=1,
            lifecycle=lifecycle,
            created_at=decision.evaluated_at,
            updated_at=decision.evaluated_at,
            terminal_at=decision.evaluated_at if lifecycle in _TERMINAL_LIFECYCLES else None,
            lifecycle_history=[transition],
            **self._decision_values(candidate, decision),
        )

    def _update_decision_record(
        self,
        existing: SignalRecord,
        candidate: ScannerCandidate,
        decision: SignalDecision,
        lifecycle: SignalLifecycle,
    ) -> SignalRecord:
        values = self._decision_values(candidate, decision)
        values["lifecycle"] = lifecycle
        changed = any(getattr(existing, key) != value for key, value in values.items())
        if not changed:
            return existing

        history = list(existing.lifecycle_history)
        terminal_at = existing.terminal_at
        if lifecycle is not existing.lifecycle:
            history.append(
                SignalTransition(
                    sequence=len(history) + 1,
                    lifecycle=lifecycle,
                    changed_at=decision.evaluated_at,
                    reason=f"DECISION_{decision.decision_status.value}",
                )
            )
            terminal_at = (
                decision.evaluated_at if lifecycle in _TERMINAL_LIFECYCLES else terminal_at
            )
        values.update(
            {
                "version": existing.version + 1,
                "updated_at": decision.evaluated_at,
                "terminal_at": terminal_at,
                "lifecycle_history": history,
            }
        )
        return existing.model_copy(update=values)

    def _decision_values(
        self,
        candidate: ScannerCandidate,
        decision: SignalDecision,
    ) -> dict[str, Any]:
        source_run_id = candidate.evidence.get("source_run_id")
        stop_provider = getattr(self._scanner, "risk_stop_price", None)
        stop_loss_price = (
            stop_provider(candidate.candidate_id) if callable(stop_provider) else None
        )
        audit_codes = list(
            dict.fromkeys(
                [
                    *candidate.audit_codes,
                    *decision.rejection_reasons,
                    *decision.watch_reasons,
                ]
            )
        )
        return {
            "symbol": decision.symbol,
            "direction": decision.direction,
            "setup": decision.setup,
            "setup_name": decision.setup_name,
            "scanner_lifecycle": candidate.lifecycle,
            "decision_status": decision.decision_status,
            "entry_trigger_status": decision.entry_trigger_status,
            "selected": decision.selected,
            "ready": decision.ready,
            "rejection_reasons": list(decision.rejection_reasons),
            "watch_reasons": list(decision.watch_reasons),
            "strategy_reasons": list(decision.strategy_reasons),
            "source_snapshot_version": decision.source_snapshot_version,
            "decision_fresh": decision.fresh,
            "risk_reward": decision.risk_reward,
            "grade": decision.grade,
            "score": decision.score,
            "confidence": decision.confidence,
            "entry_ready": candidate.entry_ready,
            "entry_trigger_price": candidate.entry_trigger_price,
            "stop_loss_price": stop_loss_price,
            "reference_close_time": candidate.reference_close_time,
            "setup_confirmed_at": candidate.setup_confirmed_at,
            "expires_at": decision.expires_at,
            "qualification_expires_at": (
                decision.expires_at
                if decision.decision_status is SignalDecisionStatus.READY
                else None
            ),
            "evaluated_at": decision.evaluated_at,
            "source_run_id": source_run_id if isinstance(source_run_id, str) else None,
            "universe_rank": candidate.universe_rank,
            "quote_volume": candidate.quote_volume,
            "spread_bps": candidate.spread_bps,
            "accepted_reasons": list(candidate.accepted_reasons),
            "audit_codes": audit_codes,
        }

    def _prune_decision_records(self) -> None:
        overflow = len(self._records_by_decision) - self._record_limit
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
            self._records_by_decision.values(),
            key=lambda record: (
                prune_rank[record.lifecycle],
                record.terminal_at or record.updated_at or record.evaluated_at,
                record.signal_id,
            ),
        )
        for record in records[:overflow]:
            decision_key = record.decision_key or record.candidate_id
            self._records_by_decision.pop(decision_key, None)
            self._decision_by_signal.pop(record.signal_id, None)

    @staticmethod
    def _decision_lifecycle(decision: SignalDecision) -> SignalLifecycle:
        mapping = {
            SignalDecisionStatus.READY: SignalLifecycle.ACTIVE,
            SignalDecisionStatus.NEAR_SETUP: SignalLifecycle.WATCH,
            SignalDecisionStatus.REJECTED: SignalLifecycle.REJECTED,
        }
        return mapping[decision.decision_status]

    @staticmethod
    def _signal_id(decision_key: str) -> str:
        return hashlib.sha256(f"astraforge-signal-v2:{decision_key}".encode()).hexdigest()


class PersistentDecisionSignalService(DecisionBackedSignalService):
    """Restart-safe decision deduplication and lifecycle recovery using SignalRow."""

    def __init__(
        self,
        scanner_service: ScannerService,
        decision_engine: SignalDecisionEngine,
        repositories: TradingStateRepositories,
        *,
        record_limit: int = SIGNAL_RECORD_LIMIT,
    ) -> None:
        self._repositories = repositories
        super().__init__(scanner_service, decision_engine, record_limit=record_limit)
        self._recover()

    def signals(self) -> SignalRecordList:
        result = super().signals()
        self._persist(result.signals)
        return result

    def status(self) -> SignalStatusResponse:
        result = super().status()
        self._persist(self._ordered_records())
        return result

    def mark_risk_blocked(
        self,
        signal_id: str,
        *,
        reason: str,
        changed_at: datetime | None = None,
    ) -> SignalRecord | None:
        result = super().mark_risk_blocked(
            signal_id,
            reason=reason,
            changed_at=changed_at,
        )
        if result is not None:
            self._persist([result])
        return result

    def _recover(self) -> None:
        with self._repositories.persistence.transaction() as session:
            rows = list(session.scalars(select(SignalRow).order_by(SignalRow.created_at)))
        for row in rows:
            record = SignalRecord.model_validate_json(row.payload_json)
            decision_key = record.decision_key or record.candidate_id
            self._records_by_decision[decision_key] = record
            self._decision_by_signal[record.signal_id] = decision_key

    def _persist(self, records: list[SignalRecord]) -> None:
        for record in records:
            payload = _payload(record)
            with self._repositories.persistence.transaction() as session:
                row = session.get(SignalRow, record.signal_id)
                if row is None:
                    row = SignalRow(
                        signal_id=record.signal_id,
                        lifecycle=record.lifecycle.value,
                        payload_json=_json(payload),
                        created_at=record.created_at,
                        updated_at=record.updated_at or record.evaluated_at,
                    )
                    session.add(row)
                else:
                    row.lifecycle = record.lifecycle.value
                    row.payload_json = _json(payload)
                    row.updated_at = record.updated_at or record.evaluated_at
                for transition in record.lifecycle_history:
                    self._repositories.append_signal_lifecycle(
                        event_id=f"{record.signal_id}:{transition.sequence}",
                        signal_id=record.signal_id,
                        version=transition.sequence,
                        lifecycle=transition.lifecycle.value,
                        audit_code=transition.reason,
                        payload=transition.model_dump(mode="json"),
                        changed_at=transition.changed_at,
                        session=session,
                    )
