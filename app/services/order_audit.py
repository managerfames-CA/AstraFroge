"""Durable exchange-authoritative order audit and field progression."""

from __future__ import annotations

import asyncio
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
from app.persistence.models import ExchangeOrderRow, FillRow
from app.persistence.repositories import TradingStateRepositories
from app.schemas.execution import (
    DemoTradeCloseReason,
    DemoTradeRecord,
    DemoTradeRecordList,
)
from app.schemas.order_audit import (
    OrderAuditFinding,
    OrderAuditRecord,
    OrderAuditRecordList,
    OrderAuditRole,
    OrderAuditState,
    OrderAuditStatusResponse,
)
from app.schemas.scanner import ScannerDirection
from app.services.recovery import AutomationRecoveryGate

_SCHEMA_VERSION = 1
_ALLOWED_STATUSES = frozenset(
    {
        "NEW",
        "PARTIALLY_FILLED",
        "FILLED",
        "FINISHED",
        "CANCELED",
        "CANCELLED",
        "EXPIRED",
        "REJECTED",
    }
)
_TERMINAL_STATUSES = frozenset(
    {"FILLED", "FINISHED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
)


class OrderAuditClient(Protocol):
    """Binance Demo read surface required for order audit verification."""

    def query_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]: ...

    def query_algo_order(
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


class OrderAuditTradeSource(Protocol):
    """Tracked trade source used to discover every expected order."""

    def trades(self) -> DemoTradeRecordList: ...


class OrderAuditVerificationError(RuntimeError):
    """Stable reason why an order audit field set could not be proved."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _Fill:
    exchange_trade_id: str
    quantity: Decimal
    price: Decimal
    filled_at: datetime


@dataclass(frozen=True)
class _Evidence:
    role: OrderAuditRole
    source: str
    client_order_id: str
    exchange_order_id: str
    actual_order_id: str | None
    requested_quantity: Decimal
    executed_quantity: Decimal
    average_fill_price: Decimal | None
    status: str
    fills: tuple[_Fill, ...]


class OrderAuditService:
    """Continuously persist complete verified order identities and economics."""

    def __init__(
        self,
        trade_source: OrderAuditTradeSource,
        client: OrderAuditClient | None,
        repositories: TradingStateRepositories | None,
        recovery_gate: AutomationRecoveryGate,
        *,
        interval_seconds: float = 15.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Order audit interval must be positive")
        self._trade_source = trade_source
        self._client = client
        self._repositories = repositories
        self._gate = recovery_gate
        self._interval_seconds = interval_seconds
        self._now = now_provider or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._latest = OrderAuditStatusResponse(
            state=OrderAuditState.NOT_RUN,
            checked_at=self._now(),
            tracked_trade_count=0,
            audited_order_count=0,
            entry_order_count=0,
            protective_order_count=0,
            manual_close_order_count=0,
            blocking=True,
            findings=[
                OrderAuditFinding(
                    code="ORDER_AUDIT_NOT_RUN",
                    message="Order audit reconciliation has not run",
                )
            ],
        )

    def latest(self) -> OrderAuditStatusResponse:
        """Return the latest immutable audit summary."""

        with self._lock:
            return self._latest.model_copy(deep=True)

    async def run_forever(self) -> None:
        """Continue observing order evidence even after automation fails closed."""

        while True:
            await asyncio.to_thread(self.reconcile)
            await asyncio.sleep(self._interval_seconds)

    def reconcile(self) -> OrderAuditStatusResponse:
        """Verify and persist every expected order field set."""

        checked_at = self._now()
        trades = self._trade_source.trades().trades
        if self._client is None or self._repositories is None:
            return self._unavailable(
                checked_at,
                len(trades),
                "ORDER_AUDIT_DURABILITY_UNAVAILABLE",
                "Binance Demo access and durable persistence are required",
            )

        findings: list[OrderAuditFinding] = []
        audited = 0
        entry_count = 0
        protective_count = 0
        manual_count = 0
        for trade in trades:
            try:
                evidences = self._evidence_for_trade(trade, self._client, checked_at)
                for evidence in evidences:
                    self._persist(trade, evidence, self._repositories, checked_at)
                    audited += 1
                    if evidence.role is OrderAuditRole.ENTRY:
                        entry_count += 1
                    elif evidence.role is OrderAuditRole.MANUAL_CLOSE:
                        manual_count += 1
                    else:
                        protective_count += 1
            except BinanceDemoPrivateClientError:
                findings.append(
                    self._finding(
                        "ORDER_AUDIT_EXCHANGE_UNAVAILABLE",
                        "Binance Demo order audit evidence is unavailable",
                        trade,
                    )
                )
            except OrderAuditVerificationError as exc:
                findings.append(self._finding(exc.code, exc.message, trade))
            except Exception:
                findings.append(
                    self._finding(
                        "ORDER_AUDIT_INVALID",
                        "Order audit verification failed closed",
                        trade,
                    )
                )

        blocking = bool(findings)
        report = OrderAuditStatusResponse(
            state=OrderAuditState.BLOCKED if blocking else OrderAuditState.READY,
            checked_at=checked_at,
            tracked_trade_count=len(trades),
            audited_order_count=audited,
            entry_order_count=entry_count,
            protective_order_count=protective_count,
            manual_close_order_count=manual_count,
            blocking=blocking,
            findings=findings,
        )
        self._publish(report)
        if blocking:
            self._gate.fail("ORDER_AUDIT_UNSAFE")
        return report

    def records(self) -> OrderAuditRecordList:
        """Return canonical durable records and reject malformed legacy state."""

        repositories = self._repositories
        if repositories is None:
            finding = OrderAuditFinding(
                code="ORDER_AUDIT_DURABILITY_UNAVAILABLE",
                message="Durable order audit persistence is unavailable",
            )
            return OrderAuditRecordList(
                count=0,
                records=[],
                state=OrderAuditState.UNAVAILABLE,
                blocking=True,
                findings=[finding],
            )

        records: list[OrderAuditRecord] = []
        findings: list[OrderAuditFinding] = []
        with repositories.persistence.transaction() as session:
            rows = list(
                session.scalars(
                    select(ExchangeOrderRow).order_by(
                        ExchangeOrderRow.created_at,
                        ExchangeOrderRow.order_id,
                    )
                )
            )
            for row in rows:
                try:
                    records.append(self._record(row))
                except OrderAuditVerificationError as exc:
                    findings.append(
                        OrderAuditFinding(
                            code=exc.code,
                            message=exc.message,
                            trade_id=row.trade_id,
                            symbol=row.symbol,
                            client_order_id=row.client_order_id,
                        )
                    )
        state = OrderAuditState.BLOCKED if findings else OrderAuditState.READY
        return OrderAuditRecordList(
            count=len(records),
            records=records,
            state=state,
            blocking=bool(findings),
            findings=findings,
        )

    def _evidence_for_trade(
        self,
        trade: DemoTradeRecord,
        client: OrderAuditClient,
        checked_at: datetime,
    ) -> list[_Evidence]:
        start_ms = int((trade.opened_at - timedelta(minutes=5)).timestamp() * 1000)
        end_anchor = trade.closed_at or checked_at
        end_ms = int((end_anchor + timedelta(minutes=5)).timestamp() * 1000)
        payloads = client.user_trades(
            symbol=trade.symbol,
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            limit=1000,
        )
        if len(payloads) >= 1000:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_FILL_WINDOW_TRUNCATED",
                "Order audit fill history reached the exchange limit",
            )

        entry = self._regular_evidence(
            trade,
            client.query_order(
                symbol=trade.symbol,
                orig_client_order_id=trade.client_order_id,
            ),
            payloads,
            role=OrderAuditRole.ENTRY,
            expected_client_id=trade.client_order_id,
            expected_exchange_id=trade.exchange_order_id,
            requested_fallback=trade.requested_quantity,
        )
        stop = self._algo_evidence(
            trade,
            client.query_algo_order(
                symbol=trade.symbol,
                orig_client_order_id=trade.stop_client_order_id,
            ),
            payloads,
            role=OrderAuditRole.STOP_LOSS,
            expected_client_id=trade.stop_client_order_id,
            expected_exchange_id=trade.stop_order_id,
            requested_fallback=trade.executed_quantity,
        )
        take = self._algo_evidence(
            trade,
            client.query_algo_order(
                symbol=trade.symbol,
                orig_client_order_id=trade.take_profit_client_order_id,
            ),
            payloads,
            role=OrderAuditRole.TAKE_PROFIT,
            expected_client_id=trade.take_profit_client_order_id,
            expected_exchange_id=trade.take_profit_order_id,
            requested_fallback=trade.executed_quantity,
        )
        evidences = [entry, stop, take]
        if trade.closed_reason in {
            DemoTradeCloseReason.MANUAL_CLOSE,
            DemoTradeCloseReason.INVALIDATED,
        }:
            close_client_id = f"af-m-{trade.signal_id[:20]}"
            close_requested = trade.executed_quantity - trade.protective_exit_filled_quantity
            if close_requested <= 0:
                raise OrderAuditVerificationError(
                    "ORDER_AUDIT_MANUAL_REQUEST_INVALID",
                    "Manual close requested quantity is not positive",
                )
            evidences.append(
                self._regular_evidence(
                    trade,
                    client.query_order(
                        symbol=trade.symbol,
                        orig_client_order_id=close_client_id,
                    ),
                    payloads,
                    role=OrderAuditRole.MANUAL_CLOSE,
                    expected_client_id=close_client_id,
                    expected_exchange_id=None,
                    requested_fallback=close_requested,
                )
            )
        return evidences

    def _regular_evidence(
        self,
        trade: DemoTradeRecord,
        payload: dict[str, Any],
        fill_payloads: list[dict[str, Any]],
        *,
        role: OrderAuditRole,
        expected_client_id: str,
        expected_exchange_id: str | None,
        requested_fallback: Decimal,
    ) -> _Evidence:
        client_id = self._text(payload.get("clientOrderId"))
        exchange_id = self._text(payload.get("orderId"))
        status = self._status(payload)
        if client_id != expected_client_id or exchange_id is None:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_ORDER_IDENTITY_INVALID",
                "Regular order identity does not match durable trade state",
            )
        if expected_exchange_id is not None and exchange_id != expected_exchange_id:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_ORDER_IDENTITY_INVALID",
                "Regular exchange order identity changed",
            )
        requested = self._requested_quantity(payload, requested_fallback)
        expected_side = self._expected_side(trade, role)
        self._validate_side(payload, expected_side)
        fills = self._fills_for_order(
            fill_payloads,
            symbol=trade.symbol,
            order_id=exchange_id,
            expected_side=expected_side,
        )
        executed, average = self._execution_economics(payload, fills)
        self._validate_economics(
            role=role,
            requested=requested,
            executed=executed,
            average=average,
            status=status,
            actual_order_id=exchange_id,
            trade=trade,
        )
        return _Evidence(
            role=role,
            source="verified_regular_order_and_fills",
            client_order_id=client_id,
            exchange_order_id=exchange_id,
            actual_order_id=exchange_id,
            requested_quantity=requested,
            executed_quantity=executed,
            average_fill_price=average,
            status=status,
            fills=tuple(fills),
        )

    def _algo_evidence(
        self,
        trade: DemoTradeRecord,
        payload: dict[str, Any],
        fill_payloads: list[dict[str, Any]],
        *,
        role: OrderAuditRole,
        expected_client_id: str,
        expected_exchange_id: str,
        requested_fallback: Decimal,
    ) -> _Evidence:
        client_id = self._text(payload.get("clientOrderId"))
        exchange_id = self._text(payload.get("orderId"))
        actual_order_id = self._text(payload.get("actualOrderId"))
        status = self._status(payload)
        if client_id != expected_client_id or exchange_id != expected_exchange_id:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_ALGO_IDENTITY_INVALID",
                "Protective Algo identity does not match durable trade state",
            )
        requested = self._requested_quantity(payload, requested_fallback)
        expected_side = self._expected_side(trade, role)
        self._validate_side(payload, expected_side)
        fills = (
            self._fills_for_order(
                fill_payloads,
                symbol=trade.symbol,
                order_id=actual_order_id,
                expected_side=expected_side,
            )
            if actual_order_id is not None
            else []
        )
        executed, average = self._execution_economics(payload, fills)
        self._validate_economics(
            role=role,
            requested=requested,
            executed=executed,
            average=average,
            status=status,
            actual_order_id=actual_order_id,
            trade=trade,
        )
        return _Evidence(
            role=role,
            source="verified_algo_order_actual_order_and_fills",
            client_order_id=client_id,
            exchange_order_id=exchange_id,
            actual_order_id=actual_order_id,
            requested_quantity=requested,
            executed_quantity=executed,
            average_fill_price=average,
            status=status,
            fills=tuple(fills),
        )

    def _persist(
        self,
        trade: DemoTradeRecord,
        evidence: _Evidence,
        repositories: TradingStateRepositories,
        verified_at: datetime,
    ) -> None:
        order_id = self._order_id(trade, evidence)
        with repositories.persistence.transaction() as session:
            row = session.scalar(
                select(ExchangeOrderRow)
                .where(ExchangeOrderRow.order_id == order_id)
                .with_for_update()
            )
            if row is None:
                row = ExchangeOrderRow(
                    order_id=order_id,
                    signal_id=trade.signal_id,
                    trade_id=trade.trade_id,
                    client_order_id=evidence.client_order_id,
                    exchange_order_id=evidence.exchange_order_id,
                    symbol=trade.symbol,
                    status=evidence.status,
                    quantity_text=self._decimal_text(evidence.executed_quantity),
                    average_price_text=self._decimal_text(evidence.average_fill_price),
                    payload_json="{}",
                    created_at=verified_at,
                    updated_at=verified_at,
                )
                session.add(row)
            else:
                self._validate_row_identity(row, trade, evidence)
                self._validate_progression(row, evidence)

            exchange_trade_ids: list[str] = []
            for fill in evidence.fills:
                self._persist_fill(session, order_id, fill)
                exchange_trade_ids.append(fill.exchange_trade_id)

            payload = {
                "schema_version": _SCHEMA_VERSION,
                "role": evidence.role.value,
                "source": evidence.source,
                "client_order_id": evidence.client_order_id,
                "exchange_order_id": evidence.exchange_order_id,
                "actual_order_id": evidence.actual_order_id,
                "requested_quantity": self._decimal_text(evidence.requested_quantity),
                "executed_quantity": self._decimal_text(evidence.executed_quantity),
                "average_fill_price": self._decimal_text(evidence.average_fill_price),
                "final_status": evidence.status,
                "exchange_trade_ids": sorted(exchange_trade_ids),
                "verified_at": verified_at.isoformat(),
            }
            row.signal_id = trade.signal_id
            row.trade_id = trade.trade_id
            row.client_order_id = evidence.client_order_id
            row.exchange_order_id = evidence.exchange_order_id
            row.symbol = trade.symbol
            row.status = evidence.status
            row.quantity_text = self._decimal_text(evidence.executed_quantity)
            row.average_price_text = self._decimal_text(evidence.average_fill_price)
            row.payload_json = self._json(payload)
            row.updated_at = verified_at
            session.flush()

    def _validate_row_identity(
        self,
        row: ExchangeOrderRow,
        trade: DemoTradeRecord,
        evidence: _Evidence,
    ) -> None:
        if (
            row.client_order_id != evidence.client_order_id
            or row.symbol != trade.symbol
            or row.signal_id not in {None, trade.signal_id}
            or row.trade_id not in {None, trade.trade_id}
            or row.exchange_order_id not in {None, evidence.exchange_order_id}
        ):
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_DURABLE_IDENTITY_CONFLICT",
                "Durable order identity conflicts with verified exchange evidence",
            )

    def _validate_progression(
        self,
        row: ExchangeOrderRow,
        evidence: _Evidence,
    ) -> None:
        payload = self._payload(row)
        if payload.get("schema_version") != _SCHEMA_VERSION:
            return
        role = self._text(payload.get("role"))
        client_id = self._text(payload.get("client_order_id"))
        exchange_id = self._text(payload.get("exchange_order_id"))
        actual_id = self._text(payload.get("actual_order_id"))
        requested = self._positive_decimal(payload.get("requested_quantity"))
        executed = self._non_negative_decimal(payload.get("executed_quantity"))
        average = self._optional_positive_decimal(payload.get("average_fill_price"))
        status = self._text(payload.get("final_status"))
        old_fill_ids = self._string_list(payload.get("exchange_trade_ids"))
        if (
            role != evidence.role.value
            or client_id != evidence.client_order_id
            or exchange_id != evidence.exchange_order_id
            or requested != evidence.requested_quantity
        ):
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_IMMUTABLE_FIELD_CHANGED",
                "Immutable durable order audit fields changed",
            )
        if actual_id is not None and actual_id != evidence.actual_order_id:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_ACTUAL_ORDER_ID_CHANGED",
                "Actual exchange order identity changed",
            )
        if evidence.executed_quantity < executed:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_EXECUTED_QUANTITY_REGRESSION",
                "Executed quantity regressed",
            )
        if evidence.executed_quantity == executed and average != evidence.average_fill_price:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_AVERAGE_PRICE_CONFLICT",
                "Average fill price changed without new executed quantity",
            )
        if status is None or not self._status_transition_allowed(status, evidence.status):
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_STATUS_REGRESSION",
                "Exchange order status regressed or changed after terminal state",
            )
        new_fill_ids = {fill.exchange_trade_id for fill in evidence.fills}
        if not set(old_fill_ids).issubset(new_fill_ids):
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_FILL_EVIDENCE_REGRESSION",
                "Previously recorded exchange fills disappeared",
            )

    @classmethod
    def _persist_fill(cls, session: Any, order_id: str, fill: _Fill) -> None:
        order = session.get(ExchangeOrderRow, order_id)
        symbol = order.symbol if order is not None else "BTCUSDT"
        existing = session.scalar(
            select(FillRow).where(
                FillRow.account_scope == "BINANCE_DEMO",
                FillRow.symbol == symbol,
                FillRow.exchange_trade_id == fill.exchange_trade_id,
            )
        )
        if existing is not None:
            if (
                existing.order_id != order_id
                or cls._positive_decimal(existing.quantity_text) != fill.quantity
                or cls._positive_decimal(existing.price_text) != fill.price
            ):
                raise OrderAuditVerificationError(
                    "ORDER_AUDIT_FILL_IDENTITY_CONFLICT",
                    "Exchange fill identity conflicts with durable state",
                )
            return
        fill_key = f"BINANCE_DEMO:{symbol}:{fill.exchange_trade_id}"
        fill_id = hashlib.sha256(
            f"ORDER_AUDIT_FILL:{fill_key}".encode()
        ).hexdigest()
        session.add(
            FillRow(
                fill_id=fill_id,
                order_id=order_id,
                account_scope="BINANCE_DEMO",
                symbol=symbol,
                exchange_trade_id=fill.exchange_trade_id,
                quantity_text=cls._decimal_text(fill.quantity) or "0",
                price_text=cls._decimal_text(fill.price) or "0",
                commission_text=None,
                payload_json=cls._json(
                    {
                        "source": "verified_binance_user_trade",
                        "exchange_trade_id": fill.exchange_trade_id,
                    }
                ),
                filled_at=fill.filled_at,
            )
        )

    def _record(self, row: ExchangeOrderRow) -> OrderAuditRecord:
        payload = self._payload(row)
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_LEGACY_ROW",
                "Durable order row has not been canonicalized by BE-14",
            )
        try:
            role = OrderAuditRole(str(payload.get("role")))
        except ValueError as exc:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_ROLE_INVALID",
                "Durable order role is invalid",
            ) from exc
        client_id = self._required_text(payload.get("client_order_id"))
        exchange_id = self._required_text(payload.get("exchange_order_id"))
        actual_id = self._text(payload.get("actual_order_id"))
        requested = self._positive_decimal(payload.get("requested_quantity"))
        executed = self._non_negative_decimal(payload.get("executed_quantity"))
        average = self._optional_positive_decimal(payload.get("average_fill_price"))
        status = self._required_text(payload.get("final_status"))
        source = self._required_text(payload.get("source"))
        fill_ids = self._string_list(payload.get("exchange_trade_ids"))
        verified_at = self._timestamp_text(payload.get("verified_at"))
        if (
            row.client_order_id != client_id
            or row.exchange_order_id != exchange_id
            or row.status != status
            or self._non_negative_decimal(row.quantity_text) != executed
            or self._optional_positive_decimal(row.average_price_text) != average
        ):
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_ROW_PARITY_INVALID",
                "Durable order columns do not match canonical audit payload",
            )
        self._validate_economics(
            role=role,
            requested=requested,
            executed=executed,
            average=average,
            status=status,
            actual_order_id=actual_id,
        )
        return OrderAuditRecord(
            order_id=row.order_id,
            signal_id=row.signal_id,
            trade_id=row.trade_id,
            role=role,
            symbol=row.symbol,
            client_order_id=client_id,
            exchange_order_id=exchange_id,
            actual_order_id=actual_id,
            requested_quantity=requested,
            executed_quantity=executed,
            average_fill_price=average,
            final_status=status,
            exchange_trade_ids=fill_ids,
            source=source,
            verified_at=verified_at,
            created_at=self._aware(row.created_at),
            updated_at=self._aware(row.updated_at),
        )

    @classmethod
    def _execution_economics(
        cls,
        payload: dict[str, Any],
        fills: list[_Fill],
    ) -> tuple[Decimal, Decimal | None]:
        fill_quantity = sum((fill.quantity for fill in fills), Decimal("0"))
        average = (
            sum((fill.quantity * fill.price for fill in fills), Decimal("0"))
            / fill_quantity
            if fill_quantity > 0
            else None
        )
        payload_executed = cls._optional_non_negative_decimal(payload.get("executedQty"))
        payload_average = cls._optional_positive_decimal(payload.get("avgPrice"))
        if payload_executed is not None and payload_executed != fill_quantity:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_EXECUTED_QUANTITY_MISMATCH",
                "Order executed quantity does not match verified fills",
            )
        if payload_average is not None and average is not None and payload_average != average:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_AVERAGE_PRICE_MISMATCH",
                "Order average price does not match weighted verified fills",
            )
        if fill_quantity == 0 and payload_average not in {None, Decimal("0")}:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_AVERAGE_PRICE_WITHOUT_FILL",
                "Order has an average price without verified fills",
            )
        return fill_quantity, average

    @classmethod
    def _fills_for_order(
        cls,
        payloads: list[dict[str, Any]],
        *,
        symbol: str,
        order_id: str,
        expected_side: str,
    ) -> list[_Fill]:
        result: list[_Fill] = []
        identities: set[str] = set()
        for payload in payloads:
            if cls._text(payload.get("symbol")) != symbol:
                continue
            if cls._text(payload.get("orderId")) != order_id:
                continue
            fill_id = cls._text(payload.get("id"))
            side = cls._text(payload.get("side"))
            if fill_id is None or fill_id in identities or side != expected_side:
                raise OrderAuditVerificationError(
                    "ORDER_AUDIT_FILL_IDENTITY_INVALID",
                    "Exchange fill identity, uniqueness or side is invalid",
                )
            identities.add(fill_id)
            result.append(
                _Fill(
                    exchange_trade_id=fill_id,
                    quantity=cls._positive_decimal(payload.get("qty")),
                    price=cls._positive_decimal(payload.get("price")),
                    filled_at=cls._timestamp_ms(payload.get("time")),
                )
            )
        return sorted(result, key=lambda item: (item.filled_at, item.exchange_trade_id))

    @classmethod
    def _validate_economics(
        cls,
        *,
        role: OrderAuditRole,
        requested: Decimal,
        executed: Decimal,
        average: Decimal | None,
        status: str,
        actual_order_id: str | None,
        trade: DemoTradeRecord | None = None,
    ) -> None:
        if status not in _ALLOWED_STATUSES:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_STATUS_INVALID",
                "Exchange order status is unsupported",
            )
        if executed > requested:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_OVERFILL",
                "Executed quantity exceeds requested quantity",
            )
        if executed > 0 and average is None:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_AVERAGE_PRICE_MISSING",
                "Executed order has no verified weighted-average fill price",
            )
        if executed == 0 and average is not None:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_AVERAGE_PRICE_WITHOUT_FILL",
                "Unfilled order has a non-zero average fill price",
            )
        if role in {OrderAuditRole.STOP_LOSS, OrderAuditRole.TAKE_PROFIT}:
            if executed > 0 and actual_order_id is None:
                raise OrderAuditVerificationError(
                    "ORDER_AUDIT_ACTUAL_ORDER_ID_MISSING",
                    "Executed protective Algo has no actual regular-order identity",
                )
        if status == "NEW" and executed != 0:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_NEW_STATUS_HAS_FILL",
                "NEW order has executed quantity",
            )
        if status == "PARTIALLY_FILLED" and not (Decimal("0") < executed < requested):
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_PARTIAL_STATUS_INVALID",
                "PARTIALLY_FILLED economics are invalid",
            )
        if status in {"FILLED", "FINISHED"} and executed != requested:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_TERMINAL_QUANTITY_INVALID",
                "Filled terminal order does not equal requested quantity",
            )
        if status == "REJECTED" and executed != 0:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_REJECTED_STATUS_HAS_FILL",
                "REJECTED order has executed quantity",
            )

    @staticmethod
    def _status_transition_allowed(current: str, incoming: str) -> bool:
        if current == incoming:
            return True
        if current in _TERMINAL_STATUSES:
            return False
        if current == "NEW":
            return incoming in _ALLOWED_STATUSES - {"NEW"}
        if current == "PARTIALLY_FILLED":
            return incoming in _TERMINAL_STATUSES
        return False

    @staticmethod
    def _expected_side(trade: DemoTradeRecord, role: OrderAuditRole) -> str:
        entry_side = "BUY" if trade.direction is ScannerDirection.LONG else "SELL"
        if role is OrderAuditRole.ENTRY:
            return entry_side
        return "SELL" if entry_side == "BUY" else "BUY"

    @classmethod
    def _validate_side(cls, payload: dict[str, Any], expected: str) -> None:
        side = cls._text(payload.get("side"))
        if side is not None and side != expected:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_ORDER_SIDE_INVALID",
                "Exchange order side does not match the tracked trade",
            )

    @classmethod
    def _requested_quantity(
        cls,
        payload: dict[str, Any],
        fallback: Decimal,
    ) -> Decimal:
        for key in ("origQty", "quantity"):
            value = payload.get(key)
            if value not in {None, ""}:
                requested = cls._positive_decimal(value)
                if requested != fallback:
                    raise OrderAuditVerificationError(
                        "ORDER_AUDIT_REQUESTED_QUANTITY_MISMATCH",
                        "Exchange requested quantity differs from the durable request",
                    )
                return requested
        return fallback

    @classmethod
    def _status(cls, payload: dict[str, Any]) -> str:
        status = cls._text(payload.get("status")) or cls._text(payload.get("algoStatus"))
        if status is None:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_STATUS_MISSING",
                "Exchange order status is missing",
            )
        return status

    @staticmethod
    def _order_id(trade: DemoTradeRecord, evidence: _Evidence) -> str:
        if evidence.role is OrderAuditRole.ENTRY:
            return f"entry:{evidence.exchange_order_id}"
        if evidence.role is OrderAuditRole.STOP_LOSS:
            return f"stop:{evidence.exchange_order_id}"
        if evidence.role is OrderAuditRole.TAKE_PROFIT:
            return f"take_profit:{evidence.exchange_order_id}"
        return hashlib.sha256(
            f"MANUAL_CLOSE_ORDER:{trade.trade_id}".encode()
        ).hexdigest()

    @staticmethod
    def _finding(code: str, message: str, trade: DemoTradeRecord) -> OrderAuditFinding:
        return OrderAuditFinding(
            code=code,
            message=message,
            trade_id=trade.trade_id,
            symbol=trade.symbol,
        )

    def _unavailable(
        self,
        checked_at: datetime,
        trade_count: int,
        code: str,
        message: str,
    ) -> OrderAuditStatusResponse:
        report = OrderAuditStatusResponse(
            state=OrderAuditState.UNAVAILABLE,
            checked_at=checked_at,
            tracked_trade_count=trade_count,
            audited_order_count=0,
            entry_order_count=0,
            protective_order_count=0,
            manual_close_order_count=0,
            blocking=True,
            findings=[OrderAuditFinding(code=code, message=message)],
        )
        self._publish(report)
        self._gate.fail(code)
        return report

    def _publish(self, report: OrderAuditStatusResponse) -> None:
        with self._lock:
            self._latest = report

    @staticmethod
    def _payload(row: ExchangeOrderRow) -> dict[str, Any]:
        try:
            payload = json.loads(row.payload_json)
        except json.JSONDecodeError as exc:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_PAYLOAD_INVALID",
                "Durable order audit payload is malformed",
            ) from exc
        if not isinstance(payload, dict):
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_PAYLOAD_INVALID",
                "Durable order audit payload is invalid",
            )
        return payload

    @staticmethod
    def _json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _required_text(cls, value: Any) -> str:
        text = cls._text(value)
        if text is None:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_TEXT_FIELD_INVALID",
                "Required order audit text field is missing",
            )
        return text

    @classmethod
    def _positive_decimal(cls, value: Any) -> Decimal:
        parsed = cls._finite_decimal(value)
        if parsed <= 0:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_DECIMAL_INVALID",
                "Order audit decimal must be positive",
            )
        return parsed

    @classmethod
    def _non_negative_decimal(cls, value: Any) -> Decimal:
        parsed = cls._finite_decimal(value)
        if parsed < 0:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_DECIMAL_INVALID",
                "Order audit decimal cannot be negative",
            )
        return parsed

    @classmethod
    def _optional_non_negative_decimal(cls, value: Any) -> Decimal | None:
        if value in {None, ""}:
            return None
        return cls._non_negative_decimal(value)

    @classmethod
    def _optional_positive_decimal(cls, value: Any) -> Decimal | None:
        if value in {None, "", "0", 0, Decimal("0")}:
            return None
        return cls._positive_decimal(value)

    @staticmethod
    def _finite_decimal(value: Any) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_DECIMAL_INVALID",
                "Order audit decimal is invalid",
            ) from exc
        if not parsed.is_finite():
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_DECIMAL_INVALID",
                "Order audit decimal must be finite",
            )
        return parsed

    @staticmethod
    def _decimal_text(value: Decimal | None) -> str | None:
        return format(value, "f") if value is not None else None

    @classmethod
    def _string_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_FILL_LIST_INVALID",
                "Order audit fill identity list is invalid",
            )
        result = [cls._required_text(item) for item in value]
        if len(result) != len(set(result)):
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_FILL_LIST_INVALID",
                "Order audit fill identities are duplicated",
            )
        return sorted(result)

    @staticmethod
    def _timestamp_ms(value: Any) -> datetime:
        try:
            milliseconds = int(value)
        except (TypeError, ValueError) as exc:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_FILL_TIMESTAMP_INVALID",
                "Exchange fill timestamp is invalid",
            ) from exc
        if milliseconds <= 0:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_FILL_TIMESTAMP_INVALID",
                "Exchange fill timestamp must be positive",
            )
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)

    @staticmethod
    def _timestamp_text(value: Any) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_TIMESTAMP_INVALID",
                "Order audit verification timestamp is invalid",
            ) from exc
        if parsed.tzinfo is None:
            raise OrderAuditVerificationError(
                "ORDER_AUDIT_TIMESTAMP_INVALID",
                "Order audit verification timestamp must be timezone-aware",
            )
        return parsed.astimezone(UTC)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
