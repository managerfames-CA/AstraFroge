"""Typed contracts for the Phase 4 Signal Decision Engine."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup


class SignalDecisionStatus(StrEnum):
    """Final eligibility owned exclusively by the Signal Decision Engine."""

    READY = "READY"
    NEAR_SETUP = "NEAR_SETUP"
    REJECTED = "REJECTED"


class EntryTriggerStatus(StrEnum):
    """Normalized entry-trigger state reported by deterministic Scanner facts."""

    READY = "READY"
    PENDING = "PENDING"
    INVALID = "INVALID"


class SignalDecision(BaseModel):
    """One deterministic final decision derived from strategy/scanner facts."""

    decision_key: str = Field(min_length=64, max_length=64)
    symbol: str
    direction: ScannerDirection
    setup: ScannerSetup
    setup_name: str
    decision_status: SignalDecisionStatus
    grade: ScannerGrade | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    confidence: int | None = Field(default=None, ge=0, le=100)
    risk_reward: Decimal | None = Field(default=None, ge=0)
    entry_trigger_status: EntryTriggerStatus
    selected: bool
    ready: bool
    rejection_reasons: list[str] = Field(default_factory=list)
    watch_reasons: list[str] = Field(default_factory=list)
    strategy_reasons: list[str] = Field(default_factory=list)
    source_snapshot_version: str | None = None
    evaluated_at: datetime
    expires_at: datetime
    fresh: bool

    @model_validator(mode="after")
    def validate_consistent_state(self) -> SignalDecision:
        """Reject contradictory eligibility combinations at the contract boundary."""

        if self.grade is ScannerGrade.B_PLUS and self.ready:
            raise ValueError("B+ decisions can never be ready")
        if self.grade is ScannerGrade.REJECT and self.ready:
            raise ValueError("Rejected grade can never be ready")
        if self.decision_status is SignalDecisionStatus.READY:
            if not self.ready or not self.selected:
                raise ValueError("READY decisions must be ready and selected")
            if self.entry_trigger_status is not EntryTriggerStatus.READY:
                raise ValueError("READY decisions require a ready trigger")
            if self.grade not in {ScannerGrade.A_PLUS, ScannerGrade.A}:
                raise ValueError("READY decisions require an A+/A grade")
            if self.rejection_reasons or not self.fresh:
                raise ValueError("READY decisions cannot contain blocking reasons or stale data")
        elif self.ready or self.selected:
            raise ValueError("Only READY decisions may be selected or execution-ready")
        if self.decision_status is SignalDecisionStatus.NEAR_SETUP and self.rejection_reasons:
            raise ValueError("NEAR_SETUP decisions cannot contain blocking rejection reasons")
        if self.decision_status is SignalDecisionStatus.REJECTED and not self.rejection_reasons:
            raise ValueError("REJECTED decisions require at least one blocking reason")
        return self
