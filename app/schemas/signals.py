"""Typed contracts for stable Signal identity, lifecycle, and audit history."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.scanner import (
    CandidateLifecycle,
    ScannerDirection,
    ScannerGrade,
    ScannerSetup,
)
from app.schemas.signal_decision import EntryTriggerStatus, SignalDecisionStatus


class SignalEngineState(StrEnum):
    """Process-scoped availability of the deterministic signal engine."""

    READY = "READY"
    WAITING_FOR_SCANNER = "WAITING_FOR_SCANNER"


class SignalLifecycle(StrEnum):
    """Signal lifecycle states maintained independently from Scanner records."""

    ACTIVE = "ACTIVE"
    WATCH = "WATCH"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    REJECTED = "REJECTED"
    RISK_BLOCKED = "RISK_BLOCKED"


class SignalTransition(BaseModel):
    """One immutable lifecycle transition in a Signal record's audit history."""

    sequence: int = Field(ge=1)
    lifecycle: SignalLifecycle
    changed_at: datetime
    reason: str = Field(min_length=1, max_length=100)


class SignalRecord(BaseModel):
    """One versioned Signal record sourced from a Scanner candidate and final decision."""

    signal_id: str
    candidate_id: str
    decision_key: str | None = None
    version: int = Field(default=1, ge=1)
    symbol: str
    direction: ScannerDirection
    setup: ScannerSetup
    setup_name: str
    lifecycle: SignalLifecycle
    scanner_lifecycle: CandidateLifecycle
    decision_status: SignalDecisionStatus | None = None
    entry_trigger_status: EntryTriggerStatus | None = None
    selected: bool = False
    ready: bool = False
    rejection_reasons: list[str] = Field(default_factory=list)
    watch_reasons: list[str] = Field(default_factory=list)
    strategy_reasons: list[str] = Field(default_factory=list)
    source_snapshot_version: str | None = None
    decision_fresh: bool = False
    risk_reward: Decimal | None = Field(default=None, ge=0)
    grade: ScannerGrade | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    confidence: int | None = Field(default=None, ge=0, le=100)
    entry_ready: bool
    entry_trigger_price: Decimal
    stop_loss_price: Decimal | None = Field(default=None, gt=0)
    reference_close_time: datetime
    setup_confirmed_at: datetime
    expires_at: datetime
    qualification_expires_at: datetime | None = None
    evaluated_at: datetime
    created_at: datetime | None = None
    updated_at: datetime | None = None
    terminal_at: datetime | None = None
    source_run_id: str | None = None
    universe_rank: int = Field(ge=1)
    quote_volume: Decimal = Field(ge=0)
    spread_bps: Decimal = Field(ge=0)
    accepted_reasons: list[str] = Field(default_factory=list)
    audit_codes: list[str] = Field(default_factory=list)
    lifecycle_history: list[SignalTransition] = Field(default_factory=list)


class SignalSummary(BaseModel):
    """Frontend-friendly counts for the Signals page."""

    active_signals: int = Field(default=0, ge=0)
    a_plus_signals: int = Field(default=0, ge=0)
    a_signals: int = Field(default=0, ge=0)
    b_plus_watch: int = Field(default=0, ge=0)
    expired: int = Field(default=0, ge=0)
    risk_blocked: int = Field(default=0, ge=0)


class SignalStatusResponse(BaseModel):
    """Current deterministic signal-engine state and summary counts."""

    state: SignalEngineState
    contract_version: str = "1"
    execution_integration_available: bool = False
    signal_engine_implemented: bool = True
    scanner_required: bool = True
    scanner_state: str
    active_signal_count: int = Field(ge=0)
    watch_signal_count: int = Field(ge=0)
    terminal_signal_count: int = Field(ge=0)
    updated_at: datetime | None = None
    latest_scanner_run_at: datetime | None = None
    summary: SignalSummary


class SignalRecordList(BaseModel):
    """Filtered signal response."""

    count: int = Field(ge=0)
    signals: list[SignalRecord]
