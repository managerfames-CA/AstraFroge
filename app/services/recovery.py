"""Fail-closed startup recovery barrier for automated Binance Demo execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.persistence.database import Persistence
from app.schemas.execution import (
    DemoExecutionAccountResponse,
    DemoExecutionActivateRequest,
    DemoExecutionPlanList,
    DemoExecutionState,
    DemoExecutionStatusResponse,
    DemoPlanState,
    DemoTradeLifecycle,
    DemoTradeRecord,
    DemoTradeRecordList,
)
from app.schemas.recovery import RecoveryState
from app.schemas.scanner import CandidateLifecycle, ScannerDirection, ScannerRunStatus
from app.schemas.signals import SignalLifecycle
from app.services.execution import DemoExecutionService
from app.services.scanner import ScannerService
from app.services.signals import SignalService

_EXECUTION_LEADER_ADVISORY_LOCK_KEY = 0x4153545241464F52
_OPEN_PROTECTION_STATUSES = frozenset({"NEW", "PARTIALLY_FILLED"})


@dataclass(frozen=True)
class RecoverySnapshot:
    """Immutable observability snapshot for the automation recovery barrier."""

    recovery_state: RecoveryState
    exchange_reconciled: bool
    signals_revalidated: bool
    automation_ready: bool
    last_recovery_at: datetime | None
    recovery_error: str | None


class AutomationRecoveryGate:
    """Single process authority that decides whether new automated entries are allowed."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._state = RecoveryState.RECOVERY_REQUIRED
        self._exchange_reconciled = False
        self._signals_revalidated = False
        self._automation_ready = False
        self._last_recovery_at: datetime | None = None
        self._recovery_error: str | None = None

    def snapshot(self) -> RecoverySnapshot:
        with self._lock:
            return RecoverySnapshot(
                recovery_state=self._state,
                exchange_reconciled=self._exchange_reconciled,
                signals_revalidated=self._signals_revalidated,
                automation_ready=self._automation_ready,
                last_recovery_at=self._last_recovery_at,
                recovery_error=self._recovery_error,
            )

    def begin(self) -> None:
        with self._lock:
            self._state = RecoveryState.RECOVERING
            self._exchange_reconciled = False
            self._signals_revalidated = False
            self._automation_ready = False
            self._recovery_error = None

    def mark_exchange_reconciled(self) -> None:
        with self._lock:
            self._state = RecoveryState.EXCHANGE_RECONCILED
            self._exchange_reconciled = True

    def mark_signals_revalidated(self) -> None:
        with self._lock:
            if not self._exchange_reconciled:
                raise RuntimeError(
                    "Exchange reconciliation must complete before signal revalidation"
                )
            self._state = RecoveryState.SIGNALS_REVALIDATED
            self._signals_revalidated = True

    def mark_ready(self) -> None:
        with self._lock:
            if not self._exchange_reconciled or not self._signals_revalidated:
                raise RuntimeError("Recovery prerequisites are incomplete")
            self._state = RecoveryState.AUTOMATION_READY
            self._automation_ready = True
            self._last_recovery_at = datetime.now(UTC)
            self._recovery_error = None

    def fail(self, error: str) -> None:
        normalized = error.strip() or "RECOVERY_FAILED"
        with self._lock:
            self._state = RecoveryState.RECOVERY_FAILED
            self._automation_ready = False
            self._last_recovery_at = datetime.now(UTC)
            self._recovery_error = normalized

    def require_ready(self) -> None:
        snapshot = self.snapshot()
        if snapshot.automation_ready:
            return
        raise AppError(
            status_code=409,
            code="RECOVERY_NOT_COMPLETE",
            message=(
                "Automated Demo entry is locked until startup recovery, exchange reconciliation, "
                "and signal revalidation complete"
            ),
        )


class ExecutionLeaderLease:
    """Hold one PostgreSQL advisory lock for the single automated execution owner."""

    def __init__(self, persistence: Persistence | None) -> None:
        self._persistence = persistence
        self._connection: Connection | None = None

    @property
    def held(self) -> bool:
        return self._connection is not None

    def acquire(self) -> bool:
        if self._connection is not None:
            return True
        if self._persistence is None:
            return False
        if self._persistence.engine.dialect.name != "postgresql":
            return False
        connection = self._persistence.engine.connect()
        try:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": _EXECUTION_LEADER_ADVISORY_LOCK_KEY},
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

    def release(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": _EXECUTION_LEADER_ADVISORY_LOCK_KEY},
            )
        finally:
            connection.close()


class RecoveryPrivateClient(Protocol):
    """Minimum Binance Demo API surface required for startup reconciliation."""

    def positions(self) -> list[dict[str, Any]]: ...

    def open_orders(self) -> list[dict[str, Any]]: ...

    def open_algo_orders(self) -> list[dict[str, Any]]: ...

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]: ...

    def query_algo_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]: ...


class RecoveryFailure(RuntimeError):
    """Stable internal startup recovery failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RecoveryGuardedExecutionService(DemoExecutionService):
    """Runtime execution facade that makes one recovery gate mandatory for enabled entries."""

    def __init__(
        self,
        inner: DemoExecutionService,
        gate: AutomationRecoveryGate,
        *,
        recovery_required: bool,
    ) -> None:
        self._inner = inner
        self._gate = gate
        self._recovery_required = recovery_required

    def status(self) -> DemoExecutionStatusResponse:
        status = self._inner.status()
        recovery = self._gate.snapshot()

        integration_ready = status.execution_integration_ready
        unavailable_reason = status.execution_unavailable_reason

        if self._recovery_required:
            if not recovery.automation_ready:
                integration_ready = False
                if recovery.recovery_error:
                    unavailable_reason = f"Startup recovery failed: {recovery.recovery_error}"
                else:
                    unavailable_reason = (
                        "Startup recovery is in progress or required "
                        f"(state: {recovery.recovery_state.value})."
                    )

        return status.model_copy(
            update={
                "state": (
                    DemoExecutionState.EXECUTION_LOCKED
                    if self._recovery_required and not recovery.automation_ready
                    else status.state
                ),
                "recovery_state": recovery.recovery_state,
                "exchange_reconciled": recovery.exchange_reconciled,
                "signals_revalidated": recovery.signals_revalidated,
                "automation_ready": recovery.automation_ready,
                "last_recovery_at": recovery.last_recovery_at,
                "recovery_error": recovery.recovery_error,
                "execution_integration_ready": integration_ready,
                "execution_unavailable_reason": unavailable_reason,
            }
        )

    def plans(self) -> DemoExecutionPlanList:
        result = self._inner.plans()
        if not self._recovery_required or self._gate.snapshot().automation_ready:
            return result
        plans = []
        for plan in result.plans:
            if plan.plan_state is DemoPlanState.EXECUTABLE:
                plans.append(
                    plan.model_copy(
                        update={
                            "plan_state": DemoPlanState.BLOCKED,
                            "blocked_reason": "RECOVERY_NOT_COMPLETE",
                            "executable_now": False,
                        }
                    )
                )
            else:
                plans.append(plan)
        return DemoExecutionPlanList(count=len(plans), plans=plans)

    def trades(self) -> DemoTradeRecordList:
        return self._inner.trades()

    def account(self) -> DemoExecutionAccountResponse:
        return self._inner.account()

    def auto_execute_pending(self) -> int:
        if self._recovery_required:
            self._gate.require_ready()
        return self._inner.auto_execute_pending()

    def activate(
        self,
        signal_id: str,
        request: DemoExecutionActivateRequest | None = None,
    ) -> DemoTradeRecord:
        if self._recovery_required:
            self._gate.require_ready()
        return self._inner.activate(signal_id, request)

    def get_trade(self, trade_id: str) -> DemoTradeRecord | None:
        return self._inner.get_trade(trade_id)

    def store_trade(self, trade: DemoTradeRecord) -> DemoTradeRecord:
        return self._inner.store_trade(trade)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class StartupRecoveryCoordinator:
    """Run the ordered, fail-closed startup recovery sequence exactly once per process."""

    def __init__(
        self,
        *,
        settings: Settings,
        gate: AutomationRecoveryGate,
        leader_lease: ExecutionLeaderLease,
        scanner_service: ScannerService,
        signal_service: SignalService,
        execution_service: DemoExecutionService,
        private_client: RecoveryPrivateClient | None,
        persistence: Persistence | None,
    ) -> None:
        self._settings = settings
        self._gate = gate
        self._leader_lease = leader_lease
        self._scanner = scanner_service
        self._signals = signal_service
        self._execution = execution_service
        self._private_client = private_client
        self._persistence = persistence

    async def recover(self) -> bool:
        """Recover durable/exchange/signal state; never expose an unsafe partial-ready state."""

        self._gate.begin()
        try:
            if not self._settings.execution_enabled:
                raise RecoveryFailure("EXECUTION_DISABLED")
            if self._persistence is None:
                raise RecoveryFailure("PERSISTENCE_REQUIRED_FOR_AUTOMATION")
            if not self._settings.scanner_auto_start:
                raise RecoveryFailure("SCANNER_AUTOMATION_DISABLED")
            if not self._leader_lease.acquire():
                raise RecoveryFailure("EXECUTION_LEADER_UNAVAILABLE")

            await asyncio.to_thread(self._reconcile_exchange_state)
            self._gate.mark_exchange_reconciled()

            run = await self._scanner.run_now()
            if run.status is not ScannerRunStatus.COMPLETED or run.completed_at is None:
                raise RecoveryFailure("SCANNER_REVALIDATION_FAILED")

            self._revalidate_signals(run.run_id, run.completed_at)
            self._gate.mark_signals_revalidated()
            self._gate.mark_ready()
            return True
        except RecoveryFailure as exc:
            self._gate.fail(exc.code)
        except BinanceDemoPrivateClientError:
            self._gate.fail("EXCHANGE_RECONCILIATION_FAILED")
        except Exception:
            self._gate.fail("RECOVERY_UNEXPECTED_FAILURE")
        self._leader_lease.release()
        return False

    def close(self) -> None:
        """Release the account-scoped execution leader lease during shutdown."""

        self._leader_lease.release()

    def _reconcile_exchange_state(self) -> None:
        client = self._private_client
        if client is None:
            raise RecoveryFailure("DEMO_PRIVATE_API_NOT_CONFIGURED")

        local_open = [
            trade
            for trade in self._execution.trades().trades
            if trade.lifecycle is DemoTradeLifecycle.OPEN
        ]
        local_by_symbol: dict[str, DemoTradeRecord] = {}
        expected_algo_orders: dict[str, tuple[str, str]] = {}
        for trade in local_open:
            if trade.symbol in local_by_symbol:
                raise RecoveryFailure("DUPLICATE_LOCAL_OPEN_TRADE")
            local_by_symbol[trade.symbol] = trade
            for client_order_id, order_id in (
                (trade.stop_client_order_id, trade.stop_order_id),
                (trade.take_profit_client_order_id, trade.take_profit_order_id),
            ):
                if client_order_id in expected_algo_orders:
                    raise RecoveryFailure("DUPLICATE_LOCAL_PROTECTIVE_ORDER")
                expected_algo_orders[client_order_id] = (trade.symbol, order_id)

        regular_open_orders = client.open_orders()
        if regular_open_orders:
            raise RecoveryFailure("UNEXPECTED_OPEN_REGULAR_ORDER")

        actual_algo_orders: dict[str, tuple[str, str]] = {}
        for payload in client.open_algo_orders():
            client_order_id = str(payload.get("clientOrderId", ""))
            order_id = str(payload.get("orderId", ""))
            symbol = str(payload.get("symbol", ""))
            if not client_order_id or not order_id or not symbol:
                raise RecoveryFailure("OPEN_ALGO_ORDER_PAYLOAD_INVALID")
            if payload.get("status") not in _OPEN_PROTECTION_STATUSES:
                raise RecoveryFailure("OPEN_ALGO_ORDER_STATUS_INVALID")
            if client_order_id in actual_algo_orders:
                raise RecoveryFailure("DUPLICATE_EXCHANGE_OPEN_ALGO_ORDER")
            actual_algo_orders[client_order_id] = (symbol, order_id)

        if set(actual_algo_orders) != set(expected_algo_orders):
            raise RecoveryFailure("OPEN_ALGO_ORDER_SET_MISMATCH")
        for client_order_id, expected_identity in expected_algo_orders.items():
            if actual_algo_orders[client_order_id] != expected_identity:
                raise RecoveryFailure("OPEN_ALGO_ORDER_IDENTITY_MISMATCH")

        exchange_by_symbol: dict[str, tuple[ScannerDirection, Decimal]] = {}
        for payload in client.positions():
            symbol = str(payload.get("symbol", ""))
            if not symbol:
                raise RecoveryFailure("EXCHANGE_POSITION_PAYLOAD_INVALID")
            amount = self._decimal(payload.get("positionAmt"), "positionAmt")
            if amount == 0:
                continue
            if symbol in exchange_by_symbol:
                raise RecoveryFailure("DUPLICATE_EXCHANGE_POSITION")
            exchange_by_symbol[symbol] = (
                ScannerDirection.LONG if amount > 0 else ScannerDirection.SHORT,
                abs(amount),
            )

        if set(local_by_symbol) != set(exchange_by_symbol):
            raise RecoveryFailure("LOCAL_EXCHANGE_POSITION_MISMATCH")

        for symbol, trade in local_by_symbol.items():
            direction, quantity = exchange_by_symbol[symbol]
            if direction is not trade.direction or quantity != trade.executed_quantity:
                raise RecoveryFailure("LOCAL_EXCHANGE_POSITION_MISMATCH")

            entry = client.query_order(
                symbol=symbol,
                orig_client_order_id=trade.client_order_id,
            )
            self._verify_entry_order(entry, trade)

            stop = client.query_algo_order(
                symbol=symbol,
                orig_client_order_id=trade.stop_client_order_id,
            )
            self._verify_protective_order(
                stop,
                expected_client_order_id=trade.stop_client_order_id,
                expected_order_id=trade.stop_order_id,
            )
            take_profit = client.query_algo_order(
                symbol=symbol,
                orig_client_order_id=trade.take_profit_client_order_id,
            )
            self._verify_protective_order(
                take_profit,
                expected_client_order_id=trade.take_profit_client_order_id,
                expected_order_id=trade.take_profit_order_id,
            )

    def _revalidate_signals(self, recovery_run_id: str, recovery_at: datetime) -> None:
        records = self._signals.signals().signals
        candidates = {candidate.candidate_id: candidate for candidate in self._scanner.candidates()}
        for signal in records:
            if signal.lifecycle is not SignalLifecycle.ACTIVE:
                continue
            candidate = candidates.get(signal.candidate_id)
            if candidate is None or candidate.lifecycle is not CandidateLifecycle.QUALIFIED:
                raise RecoveryFailure("ACTIVE_SIGNAL_REVALIDATION_FAILED")
            expiry = candidate.qualification_expires_at
            if expiry is None or recovery_at >= expiry:
                raise RecoveryFailure("ACTIVE_SIGNAL_EXPIRED_DURING_RECOVERY")
            if signal.direction is not candidate.direction or signal.setup is not candidate.setup:
                raise RecoveryFailure("ACTIVE_SIGNAL_CONTRACT_MISMATCH")
            if signal.scanner_lifecycle is not candidate.lifecycle:
                raise RecoveryFailure("ACTIVE_SIGNAL_LIFECYCLE_MISMATCH")
            if signal.entry_trigger_price != candidate.entry_trigger_price:
                raise RecoveryFailure("ACTIVE_SIGNAL_PLAN_VERSION_MISMATCH")
            if signal.source_run_id != recovery_run_id:
                raise RecoveryFailure("ACTIVE_SIGNAL_NOT_CURRENT_RUN")
            stop_provider = getattr(self._scanner, "risk_stop_price", None)
            expected_stop = (
                stop_provider(candidate.candidate_id) if callable(stop_provider) else None
            )
            if signal.stop_loss_price != expected_stop:
                raise RecoveryFailure("ACTIVE_SIGNAL_PLAN_VERSION_MISMATCH")

    @staticmethod
    def _verify_entry_order(payload: dict[str, Any], trade: DemoTradeRecord) -> None:
        if (
            str(payload.get("clientOrderId", "")) != trade.client_order_id
            or str(payload.get("orderId", "")) != trade.exchange_order_id
            or payload.get("status") != "FILLED"
        ):
            raise RecoveryFailure("ENTRY_ORDER_RECONCILIATION_MISMATCH")
        executed = StartupRecoveryCoordinator._decimal(payload.get("executedQty"), "executedQty")
        if executed != trade.executed_quantity:
            raise RecoveryFailure("ENTRY_ORDER_RECONCILIATION_MISMATCH")

    @staticmethod
    def _verify_protective_order(
        payload: dict[str, Any],
        *,
        expected_client_order_id: str,
        expected_order_id: str,
    ) -> None:
        if (
            str(payload.get("clientOrderId", "")) != expected_client_order_id
            or str(payload.get("orderId", "")) != expected_order_id
            or payload.get("status") not in _OPEN_PROTECTION_STATUSES
        ):
            raise RecoveryFailure("PROTECTIVE_ORDER_RECONCILIATION_MISMATCH")

    @staticmethod
    def _decimal(value: Any, field: str) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise RecoveryFailure("EXCHANGE_POSITION_PAYLOAD_INVALID") from exc
        if not parsed.is_finite():
            raise RecoveryFailure(f"EXCHANGE_{field.upper()}_INVALID")
        return parsed
