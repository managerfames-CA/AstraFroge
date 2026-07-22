"""Exchange-authoritative partial-close, Stop Loss and Take Profit lifecycle authority."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Any, Protocol

from sqlalchemy import select

from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.persistence.models import ExecutionIntentRow, TradeRow
from app.persistence.repositories import TradingStateRepositories
from app.schemas.execution import (
    DemoTradeCloseReason,
    DemoTradeLifecycle,
    DemoTradeRecord,
    DemoTradeRecordList,
)
from app.schemas.protective_lifecycle import (
    ProtectiveLifecycleEvent,
    ProtectiveLifecycleEventType,
    ProtectiveLifecycleFinding,
    ProtectiveLifecycleReport,
    ProtectiveLifecycleState,
)
from app.schemas.scanner import ScannerDirection
from app.services.journal_cost_verification import JournalCostVerificationService
from app.services.journal_exchange_verification import (
    JournalExchangeVerificationService,
    JournalSourceVerificationError,
)
from app.services.recovery import AutomationRecoveryGate

_EVENT_OPERATION = "PROTECTIVE_LIFECYCLE"
_PARTIAL_STATUS = "PARTIALLY_FILLED"
_OPEN_STATUS = "NEW"
_FILLED_STATUSES = frozenset({"FILLED", "FINISHED"})
_CANCELLED_STATUSES = frozenset({"CANCELED", "CANCELLED", "EXPIRED", "FINISHED"})


class ProtectiveLifecycleClient(Protocol):
    """Binance Demo surfaces required to prove protective lifecycle events."""

    def positions(self) -> list[dict[str, Any]]: ...

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]: ...

    def query_algo_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]: ...

    def cancel_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]: ...

    def user_trades(
        self,
        *,
        symbol: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]: ...

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]: ...


class ProtectiveTradeSource(Protocol):
    """Durable execution surface used to publish authoritative trade state."""

    def trades(self) -> DemoTradeRecordList: ...

    def store_trade(self, trade: DemoTradeRecord) -> DemoTradeRecord: ...


class ProtectiveLifecycleVerificationError(RuntimeError):
    """Stable reason why a lifecycle transition could not be proved."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _Position:
    direction: ScannerDirection
    quantity: Decimal


@dataclass(frozen=True)
class _Fill:
    fill_id: str
    order_id: str
    quantity: Decimal
    price: Decimal
    filled_at: datetime


@dataclass(frozen=True)
class _Algo:
    kind: DemoTradeCloseReason
    client_order_id: str
    algo_order_id: str
    actual_order_id: str | None
    status: str
    executed_quantity: Decimal | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class _Observation:
    trade: DemoTradeRecord
    reason: DemoTradeCloseReason
    algo: _Algo
    sibling: _Algo
    fills: tuple[_Fill, ...]
    remaining_quantity: Decimal
    sibling_cancelled: bool

    @property
    def exit_quantity(self) -> Decimal:
        return sum((item.quantity for item in self.fills), Decimal("0"))

    @property
    def exit_notional(self) -> Decimal:
        return sum((item.quantity * item.price for item in self.fills), Decimal("0"))

    @property
    def average_exit_price(self) -> Decimal:
        return self.exit_notional / self.exit_quantity

    @property
    def full_close(self) -> bool:
        return self.remaining_quantity == 0


@dataclass(frozen=True)
class _ClosedEconomics:
    exit_price: Decimal
    gross_realized_pnl: Decimal
    net_realized_pnl: Decimal
    commission: Decimal
    funding: Decimal


class ProtectiveLifecycleVerificationService:
    """Verify and durably apply protective lifecycle transitions exactly once."""

    def __init__(
        self,
        trade_source: ProtectiveTradeSource,
        client: ProtectiveLifecycleClient | None,
        repositories: TradingStateRepositories | None,
        recovery_gate: AutomationRecoveryGate,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._trade_source = trade_source
        self._client = client
        self._repositories = repositories
        self._gate = recovery_gate
        self._now = now_provider or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._latest = ProtectiveLifecycleReport(
            state=ProtectiveLifecycleState.NOT_RUN,
            checked_at=self._now(),
            open_trade_count=0,
            verified_event_count=0,
            partial_trade_count=0,
            closed_trade_count=0,
            blocking=False,
            findings=[],
            events=[],
        )

    def latest(self) -> ProtectiveLifecycleReport:
        """Return the latest immutable lifecycle verification result."""

        with self._lock:
            return self._latest.model_copy(deep=True)

    def reconcile(self) -> ProtectiveLifecycleReport:
        """Verify exchange lifecycle evidence and apply each fill event once."""

        checked_at = self._now()
        records = self._trade_source.trades().trades
        open_trades = [trade for trade in records if trade.lifecycle is DemoTradeLifecycle.OPEN]
        pending_cleanup = [
            trade
            for trade in records
            if trade.lifecycle is DemoTradeLifecycle.CLOSED
            and trade.closed_reason
            in {DemoTradeCloseReason.STOP_LOSS, DemoTradeCloseReason.TAKE_PROFIT}
            and trade.protective_sibling_cancelled is False
        ]
        client = self._client
        repositories = self._repositories
        if client is None or repositories is None:
            return self._unavailable(
                checked_at,
                len(open_trades),
                "PROTECTIVE_LIFECYCLE_DURABILITY_UNAVAILABLE",
                "Binance Demo access and durable persistence are required",
            )

        findings: list[ProtectiveLifecycleFinding] = []
        events: list[ProtectiveLifecycleEvent] = []
        partial_count = 0
        closed_count = 0
        try:
            positions = self._position_map(client.positions())
        except (BinanceDemoPrivateClientError, ProtectiveLifecycleVerificationError) as exc:
            code = (
                exc.code
                if isinstance(exc, ProtectiveLifecycleVerificationError)
                else "PROTECTIVE_LIFECYCLE_POSITION_UNAVAILABLE"
            )
            return self._unavailable(
                checked_at,
                len(open_trades),
                code,
                "Current Binance Demo positions could not be verified",
            )

        for trade in pending_cleanup:
            try:
                cleaned = self._retry_sibling_cleanup(trade, client, repositories)
                self._trade_source.store_trade(cleaned)
            except (BinanceDemoPrivateClientError, ProtectiveLifecycleVerificationError) as exc:
                findings.append(
                    self._finding(
                        exc,
                        trade,
                        fallback="PROTECTIVE_SIBLING_CANCEL_UNVERIFIED",
                    )
                )

        for trade in open_trades:
            try:
                observation = self._observe(trade, positions, client, checked_at)
                if observation is None:
                    continue
                economics = (
                    self._closed_economics(observation, client, checked_at)
                    if observation.full_close
                    else None
                )
                authoritative, new_events = self._apply(
                    observation,
                    economics,
                    repositories,
                    checked_at,
                )
                self._trade_source.store_trade(authoritative)
                events.extend(new_events)
                if observation.full_close:
                    closed_count += 1
                    if not observation.sibling_cancelled:
                        findings.append(
                            ProtectiveLifecycleFinding(
                                code="PROTECTIVE_SIBLING_CANCEL_UNVERIFIED",
                                message=(
                                    "The position closed, but the sibling protective order "
                                    "was not confirmed cancelled"
                                ),
                                trade_id=trade.trade_id,
                                symbol=trade.symbol,
                            )
                        )
                else:
                    partial_count += 1
                    findings.append(
                        ProtectiveLifecycleFinding(
                            code="PARTIAL_CLOSE_REQUIRES_PROTECTION_REVIEW",
                            message=(
                                "A protective partial close was verified; automation remains "
                                "blocked until remaining protection is deliberately restored"
                            ),
                            trade_id=trade.trade_id,
                            symbol=trade.symbol,
                        )
                    )
            except BinanceDemoPrivateClientError:
                findings.append(
                    ProtectiveLifecycleFinding(
                        code="PROTECTIVE_LIFECYCLE_EXCHANGE_UNAVAILABLE",
                        message="Binance Demo lifecycle evidence is unavailable",
                        trade_id=trade.trade_id,
                        symbol=trade.symbol,
                    )
                )
            except ProtectiveLifecycleVerificationError as exc:
                findings.append(self._finding(exc, trade))
            except Exception:
                findings.append(
                    ProtectiveLifecycleFinding(
                        code="PROTECTIVE_LIFECYCLE_INVALID",
                        message="Protective lifecycle verification failed closed",
                        trade_id=trade.trade_id,
                        symbol=trade.symbol,
                    )
                )

        blocking = bool(findings)
        if blocking:
            state = ProtectiveLifecycleState.BLOCKED
        elif partial_count:
            state = ProtectiveLifecycleState.PARTIAL_CLOSE_VERIFIED
        elif closed_count:
            state = ProtectiveLifecycleState.CLOSED_VERIFIED
        else:
            state = ProtectiveLifecycleState.IN_SYNC
        report = ProtectiveLifecycleReport(
            state=state,
            checked_at=checked_at,
            open_trade_count=len(open_trades),
            verified_event_count=len(events),
            partial_trade_count=partial_count,
            closed_trade_count=closed_count,
            blocking=blocking,
            findings=findings,
            events=events,
        )
        self._publish(report)
        if blocking:
            self._gate.fail("PROTECTIVE_LIFECYCLE_UNSAFE")
        return report

    def _observe(
        self,
        trade: DemoTradeRecord,
        positions: dict[str, _Position],
        client: ProtectiveLifecycleClient,
        checked_at: datetime,
    ) -> _Observation | None:
        stop = self._algo(
            client.query_algo_order(
                symbol=trade.symbol,
                orig_client_order_id=trade.stop_client_order_id,
            ),
            reason=DemoTradeCloseReason.STOP_LOSS,
            expected_client_id=trade.stop_client_order_id,
            expected_algo_id=trade.stop_order_id,
        )
        take = self._algo(
            client.query_algo_order(
                symbol=trade.symbol,
                orig_client_order_id=trade.take_profit_client_order_id,
            ),
            reason=DemoTradeCloseReason.TAKE_PROFIT,
            expected_client_id=trade.take_profit_client_order_id,
            expected_algo_id=trade.take_profit_order_id,
        )
        start_ms = int((trade.opened_at - timedelta(minutes=5)).timestamp() * 1000)
        end_ms = int((checked_at + timedelta(minutes=1)).timestamp() * 1000)
        payloads = client.user_trades(
            symbol=trade.symbol,
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            limit=1000,
        )
        if len(payloads) >= 1000:
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_FILL_WINDOW_TRUNCATED",
                "Protective fill history reached the exchange limit",
            )
        stop_fills = self._fills(payloads, trade, stop)
        take_fills = self._fills(payloads, trade, take)
        if stop_fills and take_fills:
            raise ProtectiveLifecycleVerificationError(
                "CONFLICTING_PROTECTIVE_FILLS",
                "Both Stop Loss and Take Profit have fills for the same trade",
            )

        current = positions.get(trade.symbol)
        if not stop_fills and not take_fills:
            self._verify_unchanged_position(trade, current)
            return None

        algo, sibling, fills = (stop, take, stop_fills) if stop_fills else (take, stop, take_fills)
        exit_quantity = sum((item.quantity for item in fills), Decimal("0"))
        if exit_quantity > trade.executed_quantity:
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_EXIT_EXCEEDS_ENTRY_QUANTITY",
                "Protective fills exceed the verified entry quantity",
            )
        remaining = trade.executed_quantity - exit_quantity
        if algo.executed_quantity is None or algo.executed_quantity != exit_quantity:
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_ORDER_FILL_QUANTITY_MISMATCH",
                "Protective order executed quantity does not match verified fills",
            )
        if remaining > 0 and algo.status != _PARTIAL_STATUS:
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_PARTIAL_STATUS_INVALID",
                "A partial protective fill is not exchange-confirmed as partially filled",
            )
        if remaining == 0 and algo.status not in _FILLED_STATUSES:
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_TERMINAL_STATUS_INVALID",
                "A full protective close is not exchange-confirmed as terminal",
            )
        self._verify_remaining_position(trade, current, remaining)
        sibling_cancelled = False
        if remaining == 0:
            sibling_cancelled = self._cancel_sibling(trade, sibling, client)
        return _Observation(
            trade=trade,
            reason=algo.kind,
            algo=algo,
            sibling=sibling,
            fills=tuple(fills),
            remaining_quantity=remaining,
            sibling_cancelled=sibling_cancelled,
        )

    def _apply(
        self,
        observation: _Observation,
        economics: _ClosedEconomics | None,
        repositories: TradingStateRepositories,
        recorded_at: datetime,
    ) -> tuple[DemoTradeRecord, list[ProtectiveLifecycleEvent]]:
        trade = observation.trade
        with repositories.persistence.transaction() as session:
            row = session.scalar(
                select(TradeRow).where(TradeRow.trade_id == trade.trade_id).with_for_update()
            )
            if row is None:
                raise ProtectiveLifecycleVerificationError(
                    "PROTECTIVE_LIFECYCLE_TRADE_NOT_DURABLE",
                    "The tracked trade is missing from durable persistence",
                )
            durable = DemoTradeRecord.model_validate_json(row.payload_json)
            if durable.signal_id != trade.signal_id or durable.symbol != trade.symbol:
                raise ProtectiveLifecycleVerificationError(
                    "PROTECTIVE_LIFECYCLE_TRADE_IDENTITY_MISMATCH",
                    "Durable trade identity differs from the lifecycle candidate",
                )
            if durable.lifecycle is DemoTradeLifecycle.CLOSED:
                return durable, []

            event_rows = list(
                session.scalars(
                    select(ExecutionIntentRow)
                    .where(
                        ExecutionIntentRow.operation == _EVENT_OPERATION,
                        ExecutionIntentRow.signal_id == trade.signal_id,
                    )
                    .order_by(ExecutionIntentRow.created_at, ExecutionIntentRow.intent_id)
                )
            )
            existing_fill_ids: set[str] = set()
            for event_row in event_rows:
                payload = self._event_payload(event_row)
                if payload.get("trade_id") != trade.trade_id:
                    raise ProtectiveLifecycleVerificationError(
                        "PROTECTIVE_EVENT_IDENTITY_MISMATCH",
                        "A durable lifecycle event points to another trade",
                    )
                fill_id = self._text(payload.get("exchange_trade_id"))
                if fill_id is None or fill_id in existing_fill_ids:
                    raise ProtectiveLifecycleVerificationError(
                        "PROTECTIVE_EVENT_IDENTITY_INVALID",
                        "Durable lifecycle event identity is malformed or duplicated",
                    )
                existing_fill_ids.add(fill_id)

            observed_fill_ids = {item.fill_id for item in observation.fills}
            if not existing_fill_ids.issubset(observed_fill_ids):
                raise ProtectiveLifecycleVerificationError(
                    "PROTECTIVE_EVENT_EVIDENCE_MISSING",
                    "Previously durable protective fills are absent from exchange history",
                )

            new_events: list[ProtectiveLifecycleEvent] = []
            cumulative = Decimal("0")
            for fill in sorted(observation.fills, key=lambda item: (item.filled_at, item.fill_id)):
                cumulative += fill.quantity
                remaining = trade.executed_quantity - cumulative
                event_type = (
                    ProtectiveLifecycleEventType.PARTIAL_CLOSE
                    if remaining > 0
                    else ProtectiveLifecycleEventType(observation.reason.value)
                )
                event_id = self._event_id(trade.trade_id, fill.fill_id)
                event = ProtectiveLifecycleEvent(
                    event_id=event_id,
                    event_type=event_type,
                    trade_id=trade.trade_id,
                    signal_id=trade.signal_id,
                    symbol=trade.symbol,
                    close_reason=observation.reason,
                    client_order_id=observation.algo.client_order_id,
                    algo_order_id=observation.algo.algo_order_id,
                    actual_order_id=observation.algo.actual_order_id or "",
                    exchange_trade_id=fill.fill_id,
                    fill_quantity=fill.quantity,
                    fill_price=fill.price,
                    cumulative_exit_quantity=cumulative,
                    remaining_quantity=remaining,
                    filled_at=fill.filled_at,
                    recorded_at=recorded_at,
                )
                if fill.fill_id not in existing_fill_ids:
                    session.add(
                        ExecutionIntentRow(
                            intent_id=event_id,
                            operation=_EVENT_OPERATION,
                            subject_id=event_id,
                            signal_id=trade.signal_id,
                            state=event.event_type.value,
                            client_order_ids_json=self._json([observation.algo.client_order_id]),
                            payload_json=self._json(event.model_dump(mode="json")),
                            created_at=fill.filled_at,
                            updated_at=recorded_at,
                        )
                    )
                    new_events.append(event)

            fill_ids = [item.fill_id for item in observation.fills]
            order_ids = [observation.algo.actual_order_id or ""]
            last_fill = max(observation.fills, key=lambda item: (item.filled_at, item.fill_id))
            last_event_id = self._event_id(trade.trade_id, last_fill.fill_id)
            partial_event_count = sum(
                1
                for item in observation.fills
                if sum(
                    (
                        candidate.quantity
                        for candidate in observation.fills
                        if (candidate.filled_at, candidate.fill_id)
                        <= (item.filled_at, item.fill_id)
                    ),
                    Decimal("0"),
                )
                < trade.executed_quantity
            )
            updates: dict[str, Any] = {
                "remaining_quantity": observation.remaining_quantity,
                "protective_exit_filled_quantity": observation.exit_quantity,
                "protective_exit_notional_usdt": observation.exit_notional,
                "protective_exit_fill_ids": sorted(fill_ids),
                "protective_exit_order_ids": sorted(set(order_ids)),
                "protective_exit_reason": observation.reason,
                "partial_close_count": partial_event_count,
                "last_lifecycle_event_id": last_event_id,
                "last_lifecycle_event_at": last_fill.filled_at,
                "protective_sibling_cancelled": (
                    observation.sibling_cancelled if observation.full_close else None
                ),
                "exchange_position_quantity": (
                    observation.remaining_quantity if observation.remaining_quantity > 0 else None
                ),
                "updated_at": recorded_at,
            }
            if observation.full_close:
                if economics is None:
                    raise ProtectiveLifecycleVerificationError(
                        "PROTECTIVE_CLOSE_ECONOMICS_MISSING",
                        "Verified full-close economics are unavailable",
                    )
                updates.update(
                    {
                        "lifecycle": DemoTradeLifecycle.CLOSED,
                        "exit_price": economics.exit_price,
                        "gross_realized_pnl_usdt": economics.gross_realized_pnl,
                        "realized_pnl_usdt": economics.net_realized_pnl,
                        "commission_usdt": economics.commission,
                        "funding_fees_usdt": economics.funding,
                        "order_status": "FILLED",
                        "unrealized_pnl_usdt": Decimal("0"),
                        "closed_at": last_fill.filled_at,
                        "closed_reason": observation.reason,
                    }
                )
            authoritative = durable.model_copy(update=updates)
            row.lifecycle = authoritative.lifecycle.value
            row.quantity_text = format(authoritative.effective_open_quantity, "f")
            row.exit_price_text = (
                format(authoritative.exit_price, "f")
                if authoritative.exit_price is not None
                else None
            )
            row.realized_pnl_text = format(authoritative.realized_pnl_usdt, "f")
            row.payload_json = self._json(authoritative.model_dump(mode="json"))
            row.closed_at = authoritative.closed_at
            row.updated_at = authoritative.updated_at
            session.flush()
            return authoritative, new_events

    def _closed_economics(
        self,
        observation: _Observation,
        client: ProtectiveLifecycleClient,
        checked_at: datetime,
    ) -> _ClosedEconomics:
        last_fill = max(observation.fills, key=lambda item: (item.filled_at, item.fill_id))
        candidate = observation.trade.model_copy(
            update={
                "lifecycle": DemoTradeLifecycle.CLOSED,
                "remaining_quantity": Decimal("0"),
                "exit_price": observation.average_exit_price,
                "closed_at": last_fill.filled_at,
                "closed_reason": observation.reason,
                "updated_at": checked_at,
            }
        )
        try:
            source = JournalExchangeVerificationService(
                client,
                now_provider=lambda: checked_at,
            ).verify(candidate)
            costs = JournalCostVerificationService(
                client,
                now_provider=lambda: checked_at,
            ).verify(candidate, source)
        except JournalSourceVerificationError as exc:
            raise ProtectiveLifecycleVerificationError(
                f"PROTECTIVE_CLOSE_{exc.code}",
                "Full protective close economics are not exchange-verified",
            ) from exc
        return _ClosedEconomics(
            exit_price=source.close_average_price,
            gross_realized_pnl=source.gross_realized_pnl_usdt,
            net_realized_pnl=costs.net_realized_pnl_usdt,
            commission=costs.commission_usdt,
            funding=costs.funding_usdt,
        )

    def _retry_sibling_cleanup(
        self,
        trade: DemoTradeRecord,
        client: ProtectiveLifecycleClient,
        repositories: TradingStateRepositories,
    ) -> DemoTradeRecord:
        sibling_client_id, sibling_order_id = self._sibling_identity(trade)
        payload = client.query_algo_order(
            symbol=trade.symbol,
            orig_client_order_id=sibling_client_id,
        )
        sibling = self._algo(
            payload,
            reason=(
                DemoTradeCloseReason.TAKE_PROFIT
                if trade.closed_reason is DemoTradeCloseReason.STOP_LOSS
                else DemoTradeCloseReason.STOP_LOSS
            ),
            expected_client_id=sibling_client_id,
            expected_algo_id=sibling_order_id,
        )
        if sibling.status not in _CANCELLED_STATUSES:
            cancelled = client.cancel_order(
                symbol=trade.symbol,
                orig_client_order_id=sibling_client_id,
            )
            self._verified_cancel(cancelled, sibling)
        updated = trade.model_copy(
            update={
                "protective_sibling_cancelled": True,
                "updated_at": self._now(),
            }
        )
        with repositories.persistence.transaction() as session:
            row = session.scalar(
                select(TradeRow).where(TradeRow.trade_id == trade.trade_id).with_for_update()
            )
            if row is None:
                raise ProtectiveLifecycleVerificationError(
                    "PROTECTIVE_LIFECYCLE_TRADE_NOT_DURABLE",
                    "The closed trade is missing from durable persistence",
                )
            durable = DemoTradeRecord.model_validate_json(row.payload_json)
            if durable.protective_sibling_cancelled is True:
                return durable
            updated = durable.model_copy(
                update={
                    "protective_sibling_cancelled": True,
                    "updated_at": self._now(),
                }
            )
            row.payload_json = self._json(updated.model_dump(mode="json"))
            row.updated_at = updated.updated_at
        return updated

    @classmethod
    def _algo(
        cls,
        payload: dict[str, Any],
        *,
        reason: DemoTradeCloseReason,
        expected_client_id: str,
        expected_algo_id: str,
    ) -> _Algo:
        client_id = cls._text(payload.get("clientOrderId"))
        algo_id = cls._text(payload.get("orderId"))
        status = cls._text(payload.get("status")) or cls._text(payload.get("algoStatus"))
        if client_id != expected_client_id or algo_id != expected_algo_id or status is None:
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_ORDER_IDENTITY_INVALID",
                "Protective order identity does not match durable trade state",
            )
        actual_order_id = cls._text(payload.get("actualOrderId"))
        executed_quantity = cls._optional_decimal(payload.get("executedQty"))
        return _Algo(
            kind=reason,
            client_order_id=client_id,
            algo_order_id=algo_id,
            actual_order_id=actual_order_id,
            status=status,
            executed_quantity=executed_quantity,
            payload=payload,
        )

    @classmethod
    def _fills(
        cls,
        payloads: list[dict[str, Any]],
        trade: DemoTradeRecord,
        algo: _Algo,
    ) -> list[_Fill]:
        if algo.actual_order_id is None:
            return []
        expected_side = "SELL" if trade.direction is ScannerDirection.LONG else "BUY"
        result: list[_Fill] = []
        identities: set[str] = set()
        for payload in payloads:
            if cls._text(payload.get("symbol")) != trade.symbol:
                continue
            if cls._text(payload.get("orderId")) != algo.actual_order_id:
                continue
            fill_id = cls._text(payload.get("id"))
            side = cls._text(payload.get("side"))
            if fill_id is None or fill_id in identities or side != expected_side:
                raise ProtectiveLifecycleVerificationError(
                    "PROTECTIVE_FILL_IDENTITY_INVALID",
                    "Protective fill identity, uniqueness or side is invalid",
                )
            quantity = cls._positive_decimal(payload.get("qty"))
            price = cls._positive_decimal(payload.get("price"))
            filled_at = cls._timestamp(payload.get("time"))
            identities.add(fill_id)
            result.append(
                _Fill(
                    fill_id=fill_id,
                    order_id=algo.actual_order_id,
                    quantity=quantity,
                    price=price,
                    filled_at=filled_at,
                )
            )
        return sorted(result, key=lambda item: (item.filled_at, item.fill_id))

    @classmethod
    def _position_map(cls, payloads: list[dict[str, Any]]) -> dict[str, _Position]:
        result: dict[str, _Position] = {}
        for payload in payloads:
            symbol = cls._text(payload.get("symbol"))
            if symbol is None:
                raise ProtectiveLifecycleVerificationError(
                    "PROTECTIVE_POSITION_PAYLOAD_INVALID",
                    "A Binance Demo position has no symbol",
                )
            amount = cls._finite_decimal(payload.get("positionAmt"))
            if amount == 0:
                continue
            if symbol in result:
                raise ProtectiveLifecycleVerificationError(
                    "PROTECTIVE_POSITION_DUPLICATE",
                    "Binance Demo returned duplicate non-zero positions",
                )
            result[symbol] = _Position(
                direction=(ScannerDirection.LONG if amount > 0 else ScannerDirection.SHORT),
                quantity=abs(amount),
            )
        return result

    @staticmethod
    def _verify_unchanged_position(
        trade: DemoTradeRecord,
        position: _Position | None,
    ) -> None:
        expected = trade.effective_open_quantity
        if position is None:
            raise ProtectiveLifecycleVerificationError(
                "UNVERIFIED_POSITION_CLOSE",
                "The position disappeared without a verified protective fill",
            )
        if position.direction is not trade.direction:
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_POSITION_DIRECTION_MISMATCH",
                "The exchange position direction differs from the tracked trade",
            )
        if position.quantity != expected:
            raise ProtectiveLifecycleVerificationError(
                "UNVERIFIED_POSITION_REDUCTION",
                "The position quantity changed without matching protective fills",
            )

    @staticmethod
    def _verify_remaining_position(
        trade: DemoTradeRecord,
        position: _Position | None,
        remaining: Decimal,
    ) -> None:
        if remaining == 0:
            if position is not None:
                raise ProtectiveLifecycleVerificationError(
                    "PROTECTIVE_FULL_CLOSE_POSITION_REMAINS",
                    "Protective fills equal the entry quantity but a position remains",
                )
            return
        if position is None:
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_PARTIAL_POSITION_MISSING",
                "A partial protective fill has no remaining exchange position",
            )
        if position.direction is not trade.direction or position.quantity != remaining:
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_REMAINING_POSITION_MISMATCH",
                "Remaining exchange position does not match protective fill economics",
            )

    def _cancel_sibling(
        self,
        trade: DemoTradeRecord,
        sibling: _Algo,
        client: ProtectiveLifecycleClient,
    ) -> bool:
        if sibling.status in _CANCELLED_STATUSES:
            return True
        if sibling.status != _OPEN_STATUS:
            return False
        try:
            payload = client.cancel_order(
                symbol=trade.symbol,
                orig_client_order_id=sibling.client_order_id,
            )
            self._verified_cancel(payload, sibling)
        except (BinanceDemoPrivateClientError, ProtectiveLifecycleVerificationError):
            return False
        return True

    @classmethod
    def _verified_cancel(cls, payload: dict[str, Any], sibling: _Algo) -> None:
        client_id = cls._text(payload.get("clientOrderId"))
        algo_id = cls._text(payload.get("orderId"))
        status = cls._text(payload.get("status")) or cls._text(payload.get("algoStatus"))
        if (
            client_id != sibling.client_order_id
            or algo_id != sibling.algo_order_id
            or status not in _CANCELLED_STATUSES
        ):
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_SIBLING_CANCEL_INVALID",
                "Sibling protective cancellation was not exchange-confirmed",
            )

    @staticmethod
    def _sibling_identity(trade: DemoTradeRecord) -> tuple[str, str]:
        if trade.closed_reason is DemoTradeCloseReason.STOP_LOSS:
            return trade.take_profit_client_order_id, trade.take_profit_order_id
        if trade.closed_reason is DemoTradeCloseReason.TAKE_PROFIT:
            return trade.stop_client_order_id, trade.stop_order_id
        raise ProtectiveLifecycleVerificationError(
            "PROTECTIVE_CLOSE_REASON_INVALID",
            "Closed protective trade has no Stop Loss or Take Profit reason",
        )

    @staticmethod
    def _event_id(trade_id: str, fill_id: str) -> str:
        return hashlib.sha256(f"{_EVENT_OPERATION}:{trade_id}:{fill_id}".encode()).hexdigest()

    @classmethod
    def _event_payload(cls, row: ExecutionIntentRow) -> dict[str, Any]:
        try:
            payload = json.loads(row.payload_json)
        except json.JSONDecodeError as exc:
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_EVENT_PAYLOAD_INVALID",
                "Durable lifecycle event payload is malformed",
            ) from exc
        if not isinstance(payload, dict):
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_EVENT_PAYLOAD_INVALID",
                "Durable lifecycle event payload is invalid",
            )
        return payload

    @staticmethod
    def _json(payload: dict[str, Any] | list[str]) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _positive_decimal(cls, value: Any) -> Decimal:
        parsed = cls._finite_decimal(value)
        if parsed <= 0:
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_DECIMAL_INVALID",
                "Protective lifecycle quantity or price must be positive",
            )
        return parsed

    @classmethod
    def _optional_decimal(cls, value: Any) -> Decimal | None:
        if value in {None, ""}:
            return None
        parsed = cls._finite_decimal(value)
        if parsed < 0:
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_DECIMAL_INVALID",
                "Protective executed quantity cannot be negative",
            )
        return parsed

    @staticmethod
    def _finite_decimal(value: Any) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_DECIMAL_INVALID",
                "Protective lifecycle decimal is invalid",
            ) from exc
        if not parsed.is_finite():
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_DECIMAL_INVALID",
                "Protective lifecycle decimal must be finite",
            )
        return parsed

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        try:
            milliseconds = int(value)
        except (TypeError, ValueError) as exc:
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_FILL_TIMESTAMP_INVALID",
                "Protective fill timestamp is invalid",
            ) from exc
        if milliseconds <= 0:
            raise ProtectiveLifecycleVerificationError(
                "PROTECTIVE_FILL_TIMESTAMP_INVALID",
                "Protective fill timestamp must be positive",
            )
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)

    @staticmethod
    def _finding(
        exc: Exception,
        trade: DemoTradeRecord,
        *,
        fallback: str = "PROTECTIVE_LIFECYCLE_INVALID",
    ) -> ProtectiveLifecycleFinding:
        return ProtectiveLifecycleFinding(
            code=(exc.code if isinstance(exc, ProtectiveLifecycleVerificationError) else fallback),
            message=(
                exc.message
                if isinstance(exc, ProtectiveLifecycleVerificationError)
                else "Protective lifecycle verification failed closed"
            ),
            trade_id=trade.trade_id,
            symbol=trade.symbol,
        )

    def _unavailable(
        self,
        checked_at: datetime,
        open_count: int,
        code: str,
        message: str,
    ) -> ProtectiveLifecycleReport:
        report = ProtectiveLifecycleReport(
            state=ProtectiveLifecycleState.UNAVAILABLE,
            checked_at=checked_at,
            open_trade_count=open_count,
            verified_event_count=0,
            partial_trade_count=0,
            closed_trade_count=0,
            blocking=True,
            findings=[ProtectiveLifecycleFinding(code=code, message=message)],
            events=[],
        )
        self._publish(report)
        self._gate.fail(code)
        return report

    def _publish(self, report: ProtectiveLifecycleReport) -> None:
        with self._lock:
            self._latest = report
