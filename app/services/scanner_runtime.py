"""Process-scoped Scanner runtime state, testable clock, and scheduler."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.core.config import Settings, get_settings
from app.persistence.database import Persistence
from app.schemas.indicators import IndicatorSeries
from app.schemas.market import MarketCandleSeries, MarketStatus
from app.schemas.scanner import (
    CandidateLifecycle,
    ScannerAuditRecord,
    ScannerCandidate,
    ScannerDirection,
    ScannerRunStatus,
    ScannerRunSummary,
    ScannerRunType,
    ScannerSetup,
    ScannerStartSource,
    ScannerState,
    ScannerStatusResponse,
)
from app.schemas.universe import UniverseSnapshot
from app.services.scanner_base import EvaluationContext, _frame_value
from app.services.scanner_contract import (
    ACTIVE_REFRESH_INTERVAL,
    CONTRACT_VERSION,
    SCANNER_RUN_HISTORY_LIMIT,
    SCANNER_TERMINAL_CANDIDATE_LIMIT,
    SCANNER_TERMINAL_HISTORY_LIMIT,
)


class ScannerMarketProvider(Protocol):
    """Existing Market Data interface consumed by Scanner."""

    async def status(self) -> MarketStatus: ...

    async def candles(self, symbol: str, interval: str, limit: int) -> MarketCandleSeries: ...


class ScannerUniverseProvider(Protocol):
    """Existing Universe interface consumed by Scanner."""

    async def build(self) -> UniverseSnapshot: ...


class ScannerIndicatorProvider(Protocol):
    """Existing Indicator interface consumed by Scanner."""

    async def build(self, symbol: str, interval: str, limit: int) -> IndicatorSeries: ...


class ScannerClock(Protocol):
    """Testable UTC time and scheduler abstraction."""

    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemScannerClock:
    """UTC system clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


_SCANNER_SCHEDULER_ADVISORY_LOCK_KEY = 0x4153545241464F53
_SCANNER_ADVISORY_CLASS_ID = _SCANNER_SCHEDULER_ADVISORY_LOCK_KEY >> 32
_SCANNER_ADVISORY_OBJECT_ID = _SCANNER_SCHEDULER_ADVISORY_LOCK_KEY & 0xFFFFFFFF


class ScannerSchedulerLost(RuntimeError):
    """The dedicated PostgreSQL session no longer proves Scanner scheduling ownership."""


class ScannerSchedulerLease:
    """Hold one PostgreSQL advisory lock for the single Scanner scheduler owner."""

    def __init__(self, persistence: Persistence | None) -> None:
        self._persistence = persistence
        self._connection: Connection | None = None

    @property
    def held(self) -> bool:
        """Return true only when the dedicated session still proves the exact advisory lock."""
        connection = self._connection
        if connection is None:
            return False
        if connection.closed or connection.invalidated:
            self._discard_lost_connection()
            return False
        try:
            owned = bool(
                connection.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM pg_locks
                            WHERE locktype = 'advisory'
                              AND pid = pg_backend_pid()
                              AND granted
                              AND classid::bigint = :class_id
                              AND objid::bigint = :object_id
                              AND objsubid = 1
                        )
                        """
                    ),
                    {
                        "class_id": _SCANNER_ADVISORY_CLASS_ID,
                        "object_id": _SCANNER_ADVISORY_OBJECT_ID,
                    },
                ).scalar()
            )
            if not owned:
                self._discard_lost_connection()
            return owned
        except Exception:
            self._discard_lost_connection()
            return False

    def acquire(self) -> bool:
        """Acquire the single-owner Scanner scheduler advisory lock."""
        if self._connection is not None:
            if self.held:
                return True
            else:
                self.release()

        if self._persistence is None:
            return False
        if self._persistence.engine.dialect.name != "postgresql":
            return False

        connection = self._persistence.engine.connect()
        try:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": _SCANNER_SCHEDULER_ADVISORY_LOCK_KEY},
                ).scalar()
            )
        except Exception:
            connection.close()
            raise

        if not acquired:
            connection.close()
            return False

        self._connection = connection
        return True

    def validate_current_ownership(self) -> None:
        """Prove the same live DB session still owns the advisory lock."""
        connection = self._connection
        if connection is None:
            raise ScannerSchedulerLost("Scanner scheduler lease is not acquired")
        if connection.closed or connection.invalidated:
            self._discard_lost_connection()
            raise ScannerSchedulerLost("Scanner scheduler database session is unavailable")

        try:
            owned = bool(
                connection.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM pg_locks
                            WHERE locktype = 'advisory'
                              AND pid = pg_backend_pid()
                              AND granted
                              AND classid::bigint = :class_id
                              AND objid::bigint = :object_id
                              AND objsubid = 1
                        )
                        """
                    ),
                    {
                        "class_id": _SCANNER_ADVISORY_CLASS_ID,
                        "object_id": _SCANNER_ADVISORY_OBJECT_ID,
                    },
                ).scalar()
            )
        except Exception as exc:
            self._discard_lost_connection()
            raise ScannerSchedulerLost(
                "Scanner scheduler database session validation failed"
            ) from exc

        if not owned:
            self._discard_lost_connection()
            raise ScannerSchedulerLost("Scanner scheduler advisory-lock ownership was lost")

    def release(self) -> None:
        """Unlock and cleanly close the database connection."""
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            if not connection.closed and not connection.invalidated:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": _SCANNER_SCHEDULER_ADVISORY_LOCK_KEY},
                )
        except Exception:
            pass
        finally:
            try:
                connection.close()
            except Exception:
                pass

    def _discard_lost_connection(self) -> None:
        """Forget the connection after a validation loss."""
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            connection.close()
        except Exception:
            pass


def next_five_minute_boundary(value: datetime) -> datetime:
    """Return the next exchange-time five-minute boundary strictly after value."""

    normalized = value.astimezone(UTC)
    floor = normalized.replace(
        minute=(normalized.minute // 5) * 5,
        second=0,
        microsecond=0,
    )
    return floor + ACTIVE_REFRESH_INTERVAL


class ScannerRuntimeBase:
    """Process-scoped state, shared lock, dedupe memory, and scheduling behavior."""

    def __init__(
        self,
        market_service: ScannerMarketProvider,
        universe_service: ScannerUniverseProvider,
        indicator_service: ScannerIndicatorProvider,
        *,
        clock: ScannerClock | None = None,
        lease: ScannerSchedulerLease | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._market = market_service
        self._universe = universe_service
        self._indicators = indicator_service
        self._clock = clock or SystemScannerClock()
        self._state = ScannerState.OFF
        self._lock = asyncio.Lock()
        self._run_active = False
        self._candidates: dict[str, ScannerCandidate] = {}
        self._candidate_contexts: dict[str, EvaluationContext] = {}
        self._terminal_keys: set[str] = set()
        self._terminal_history: dict[tuple[str, ScannerDirection, ScannerSetup], datetime] = {}
        self._runs: list[ScannerRunSummary] = []
        self._next_full_scan_at: datetime | None = None
        self._next_refresh_at: datetime | None = None
        self._last_refresh_boundary: datetime | None = None
        self._scheduler_task: asyncio.Task[None] | None = None

        # BE-17 lease, settings & state fields
        self._settings = settings or get_settings()
        self._lease = lease or ScannerSchedulerLease(None)
        self._start_source: ScannerStartSource | None = None
        self._ownership_required: bool = False
        self._blocking_code: str | None = None
        self._blocking_reason: str | None = None
        self._last_ownership_validation_at: datetime | None = None
        self._fail_closed_state: bool = False

    def _is_postgresql_authoritative(self) -> bool:
        if self._lease._persistence is None:
            return False
        try:
            return self._lease._persistence.engine.dialect.name == "postgresql"
        except Exception:
            return False

    def status(self) -> ScannerStatusResponse:
        active = sum(
            candidate.lifecycle
            in {
                CandidateLifecycle.DETECTED,
                CandidateLifecycle.WATCH_NEAR,
                CandidateLifecycle.QUALIFIED,
            }
            for candidate in self._candidates.values()
        )
        terminal = sum(
            candidate.lifecycle
            in {
                CandidateLifecycle.REJECTED,
                CandidateLifecycle.INVALIDATED,
                CandidateLifecycle.EXPIRED,
            }
            for candidate in self._candidates.values()
        )
        scheduler_running = self._scheduler_task is not None and not self._scheduler_task.done()

        # Single query to avoid double validation and potential inconsistency
        held_status = self._lease.held

        return ScannerStatusResponse(
            state=self._state,
            contract_version=CONTRACT_VERSION,
            run_active=self._run_active,
            scheduler_running=scheduler_running,
            next_full_scan_at=self._next_full_scan_at,
            next_refresh_at=self._next_refresh_at,
            last_refresh_boundary=self._last_refresh_boundary,
            active_candidate_count=active,
            terminal_candidate_count=terminal,
            latest_run=self._runs[-1] if self._runs else None,
            auto_start_configured=self._settings.scanner_auto_start,
            start_source=self._start_source,
            ownership_required=self._ownership_required,
            ownership_held=held_status,
            is_owner=held_status,
            blocking_code=self._blocking_code,
            blocking_reason=self._blocking_reason,
            last_ownership_validation_at=self._last_ownership_validation_at,
        )

    def candidates(self) -> list[ScannerCandidate]:
        return sorted(
            self._candidates.values(),
            key=lambda item: (
                item.lifecycle is not CandidateLifecycle.QUALIFIED,
                -(item.score if item.score is not None else -1),
                -(item.confidence if item.confidence is not None else -1),
                item.universe_rank,
                item.spread_bps,
                -item.quote_volume,
                item.symbol,
                item.direction.value,
                item.setup.value,
                item.reference_close_time,
            ),
        )

    def risk_stop_price(self, candidate_id: str) -> Decimal | None:
        """Return the fixed price boundary from the approved invalidation contract."""

        candidate = self._candidates.get(candidate_id)
        context = self._candidate_contexts.get(candidate_id)
        if candidate is None or context is None:
            return None

        atr15 = _frame_value(context.s[0], "atr14")
        atr5 = _frame_value(context.e[0], "atr14")
        stop: Decimal | None = None
        try:
            if candidate.setup is ScannerSetup.TREND_PULLBACK:
                swing_low = self._evidence_decimal(candidate, "pullback_swing_low")
                swing_high = self._evidence_decimal(candidate, "pullback_swing_high")
                ema50 = _frame_value(context.s[0], "ema50")
                if candidate.direction is ScannerDirection.LONG:
                    stop = max(
                        ema50 - Decimal("0.25") * atr15,
                        swing_low - Decimal("0.10") * atr5,
                    )
                else:
                    stop = min(
                        ema50 + Decimal("0.25") * atr15,
                        swing_high + Decimal("0.10") * atr5,
                    )
            elif candidate.setup is ScannerSetup.BREAKOUT_RETEST and candidate.level is not None:
                stop = (
                    candidate.level - Decimal("0.15") * atr15
                    if candidate.direction is ScannerDirection.LONG
                    else candidate.level + Decimal("0.15") * atr15
                )
            elif (
                candidate.setup is ScannerSetup.EMA_REJECTION and candidate.selected_ema is not None
            ):
                reference_low = self._evidence_decimal(candidate, "reference_low")
                reference_high = self._evidence_decimal(candidate, "reference_high")
                if candidate.direction is ScannerDirection.LONG:
                    stop = max(
                        candidate.selected_ema - Decimal("0.20") * atr15,
                        reference_low - Decimal("0.05") * atr15,
                    )
                else:
                    stop = min(
                        candidate.selected_ema + Decimal("0.20") * atr15,
                        reference_high + Decimal("0.05") * atr15,
                    )
            elif candidate.setup is ScannerSetup.LIQUIDITY_SWEEP_REVERSAL:
                reference_low = self._evidence_decimal(candidate, "reference_low")
                reference_high = self._evidence_decimal(candidate, "reference_high")
                stop = (
                    reference_low - Decimal("0.05") * atr5
                    if candidate.direction is ScannerDirection.LONG
                    else reference_high + Decimal("0.05") * atr5
                )
            elif candidate.setup is ScannerSetup.CONTINUATION_SETUP and candidate.level is not None:
                stop = (
                    candidate.level - Decimal("0.15") * atr15
                    if candidate.direction is ScannerDirection.LONG
                    else candidate.level + Decimal("0.15") * atr15
                )
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return None

        if stop is None or not stop.is_finite() or stop <= 0:
            return None
        if candidate.direction is ScannerDirection.LONG:
            return stop if stop < candidate.entry_trigger_price else None
        return stop if stop > candidate.entry_trigger_price else None

    @staticmethod
    def _evidence_decimal(candidate: ScannerCandidate, key: str) -> Decimal:
        if key not in candidate.evidence:
            raise KeyError(key)
        value = Decimal(str(candidate.evidence[key]))
        if not value.is_finite():
            raise ValueError(key)
        return value

    def latest_run(self) -> ScannerRunSummary | None:
        return self._runs[-1] if self._runs else None

    async def start(self, source: ScannerStartSource | str = "manual") -> ScannerStatusResponse:
        """Idempotently start the recurring scanner with single-owner lease protection."""
        if self._state is ScannerState.ON:
            return self.status()

        # Parse source cleanly
        if isinstance(source, ScannerStartSource):
            self._start_source = source
        else:
            try:
                self._start_source = ScannerStartSource(source)
            except ValueError:
                self._start_source = ScannerStartSource.MANUAL

        self._blocking_code = None
        self._blocking_reason = None

        is_pg = self._is_postgresql_authoritative()

        # Fail-closed rules:
        # 1. Automatic lifespan startup ALWAYS requires PostgreSQL ownership in every environment.
        # 2. Bypassing ownership is only allowed during explicit manual/API fallback in
        #    development or test.
        if self._start_source == ScannerStartSource.LIFESPAN:
            self._ownership_required = True
        else:
            self._ownership_required = is_pg or (
                self._settings.environment in {"staging", "production"}
            )

        if self._ownership_required:
            if not is_pg:
                self._state = ScannerState.OFF
                self._fail_closed_state = True
                self._blocking_code = "PERSISTENCE_UNAVAILABLE"
                self._blocking_reason = (
                    "PostgreSQL persistence is absent, unavailable, or not authoritative"
                )
                return self.status()

            try:
                acquired = self._lease.acquire()
            except Exception:
                acquired = False

            if not acquired:
                self._state = ScannerState.OFF
                self._blocking_code = "OWNERSHIP_ACQUISITION_FAILED"
                self._blocking_reason = "Scanner scheduler lease ownership could not be acquired"
                return self.status()

            try:
                self._lease.validate_current_ownership()
                self._last_ownership_validation_at = self._clock.now()
            except Exception:
                self._state = ScannerState.OFF
                self._blocking_code = "OWNERSHIP_VALIDATION_FAILED"
                self._blocking_reason = "Scanner scheduler lease validation failed"
                return self.status()

        self._state = ScannerState.ON
        self._next_full_scan_at = self._clock.now()
        self._next_refresh_at = None
        self._ensure_scheduler()
        return self.status()

    async def stop(self) -> ScannerStatusResponse:
        self._state = ScannerState.OFF
        self._next_full_scan_at = None
        self._next_refresh_at = None
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            self._scheduler_task = None
        self._lease.release()
        return self.status()

    async def run_now(self) -> ScannerRunSummary:
        return await self.full_scan()

    def _ensure_scheduler(self) -> None:
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def _scheduler_loop(self) -> None:  # pragma: no cover - integration clock loop
        try:
            while self._state is ScannerState.ON:
                if self._is_postgresql_authoritative() and self._ownership_required:
                    try:
                        self._lease.validate_current_ownership()
                        self._last_ownership_validation_at = self._clock.now()
                    except Exception:
                        self._state = ScannerState.OFF
                        self._fail_closed_state = True
                        self._blocking_code = "SCANNER_SCHEDULER_LEADER_LOST"
                        self._blocking_reason = (
                            "Dedicated Scanner scheduler database connection "
                            "or advisory lock was lost"
                        )
                        self._next_full_scan_at = None
                        self._next_refresh_at = None
                        break

                now = self._clock.now()
                if self._next_full_scan_at is not None and now >= self._next_full_scan_at:
                    await self.full_scan()
                if self._next_refresh_at is not None and now >= self._next_refresh_at:
                    boundary = self._next_refresh_at
                    run = await self.active_refresh()
                    if run.status is not ScannerRunStatus.SKIPPED:
                        self._last_refresh_boundary = boundary
                    while boundary <= self._clock.now():
                        boundary += ACTIVE_REFRESH_INTERVAL
                    self._next_refresh_at = boundary
                await self._clock.sleep(1)
                await asyncio.sleep(0.0001)  # Always yield control to avoid event loop starvation
        except asyncio.CancelledError:
            return

    async def _skipped(self, run_type: ScannerRunType) -> ScannerRunSummary:
        now = self._clock.now()
        run = ScannerRunSummary(
            run_id=str(uuid4()),
            run_type=run_type,
            status=ScannerRunStatus.SKIPPED,
            run_started_at=now,
            completed_at=now,
            audits=[
                ScannerAuditRecord(
                    code="SCAN_ALREADY_RUNNING",
                    detail="Shared Scanner lock is active; attempt was not queued",
                )
            ],
        )
        self._append_run(run)
        return run

    def _append_run(self, run: ScannerRunSummary) -> None:
        self._runs.append(run)
        overflow = len(self._runs) - SCANNER_RUN_HISTORY_LIMIT
        if overflow > 0:
            del self._runs[:overflow]

    def _record_terminal(self, candidate: ScannerCandidate) -> None:
        self._terminal_keys.add(candidate.candidate_id)
        self._terminal_history[(candidate.symbol, candidate.direction, candidate.setup)] = (
            candidate.evaluated_at
        )
        self._candidate_contexts.pop(candidate.candidate_id, None)
        self._prune_terminal_state()

    def _prune_terminal_state(self) -> None:
        terminal_lifecycles = {
            CandidateLifecycle.REJECTED,
            CandidateLifecycle.INVALIDATED,
            CandidateLifecycle.EXPIRED,
        }
        terminal_candidates = sorted(
            (
                candidate
                for candidate in self._candidates.values()
                if candidate.lifecycle in terminal_lifecycles
            ),
            key=lambda candidate: (candidate.evaluated_at, candidate.candidate_id),
        )
        overflow = len(terminal_candidates) - SCANNER_TERMINAL_CANDIDATE_LIMIT
        for candidate in terminal_candidates[: max(0, overflow)]:
            self._candidates.pop(candidate.candidate_id, None)
            self._candidate_contexts.pop(candidate.candidate_id, None)
            # Retain the compact terminal tombstone even after the heavier payload
            # is pruned so the exact candidate lifecycle cannot silently reactivate.

        history_overflow = len(self._terminal_history) - SCANNER_TERMINAL_HISTORY_LIMIT
        if history_overflow <= 0:
            return
        oldest_history = sorted(
            self._terminal_history.items(),
            key=lambda item: (
                item[1],
                item[0][0],
                item[0][1].value,
                item[0][2].value,
            ),
        )
        for key, _ in oldest_history[:history_overflow]:
            self._terminal_history.pop(key, None)

    async def full_scan(self) -> ScannerRunSummary:
        """Implemented by the Full Universe Scan service layer."""

        raise NotImplementedError

    async def active_refresh(self) -> ScannerRunSummary:
        """Implemented by the Active Candidate Refresh service layer."""

        raise NotImplementedError
