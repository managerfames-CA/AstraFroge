"""Persistence-backed adapters around existing runtime services without changing rules."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.persistence.models import ExecutionIntentRow, PositionRow, SignalRow, TradeRow
from app.persistence.repositories import TradingStateRepositories
from app.schemas.execution import (
    DemoExecutionAccountResponse,
    DemoExecutionActivateRequest,
    DemoTradeRecord,
)
from app.schemas.risk import RiskAssessmentList, RiskStatusResponse
from app.schemas.signals import SignalRecord, SignalRecordList, SignalStatusResponse
from app.services.execution import DemoExecutionService, ExecutionPrivateClient
from app.services.risk import RiskPrivateClient, RiskService
from app.services.scanner import ScannerService
from app.services.signals import SignalService

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "apisecret",
        "authorization",
        "bearer",
        "password",
        "secret",
        "signature",
        "token",
    }
)


def _normalized_key(value: object) -> str:
    return str(value).lower().replace("-", "_")


def reject_sensitive_payload(value: object, *, path: str = "payload") -> None:
    """Reject generic payloads containing credential-like keys at any depth."""

    if isinstance(value, dict):
        for key, child in value.items():
            normalized = _normalized_key(key)
            if normalized in _SENSITIVE_KEYS or any(
                marker in normalized
                for marker in (
                    "api_key",
                    "api_secret",
                    "authorization",
                    "password",
                    "signature",
                )
            ):
                raise ValueError(f"Sensitive persistence key is not allowed: {path}.{key}")
            reject_sensitive_payload(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_sensitive_payload(child, path=f"{path}[{index}]")


def _payload(model: Any) -> dict[str, Any]:
    payload: dict[str, Any] = model.model_dump(mode="json")
    reject_sensitive_payload(payload)
    return payload


def _json(payload: dict[str, Any] | list[str]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class PersistentSignalService(SignalService):
    """Signal service with durable writes and startup recovery."""

    def __init__(
        self,
        scanner_service: ScannerService,
        repositories: TradingStateRepositories,
        *,
        record_limit: int = 1_000,
    ) -> None:
        self._repositories = repositories
        super().__init__(scanner_service, record_limit=record_limit)
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
            self._records_by_candidate[record.candidate_id] = record
            self._candidate_by_signal[record.signal_id] = record.candidate_id

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


class PersistentRiskService(RiskService):
    """Risk service that durably records each deterministic assessment result."""

    def __init__(
        self,
        signal_service: SignalService,
        settings: Settings,
        private_client: RiskPrivateClient | None,
        repositories: TradingStateRepositories,
    ) -> None:
        self._repositories = repositories
        super().__init__(signal_service, settings, private_client)

    def assessments(self) -> RiskAssessmentList:
        result = super().assessments()
        self._persist(result)
        return result

    def status(self) -> RiskStatusResponse:
        result = super().status()
        self.assessments()
        return result

    def _persist(self, result: RiskAssessmentList) -> None:
        for assessment in result.assessments:
            payload = _payload(assessment)
            digest = hashlib.sha256(_json(payload).encode()).hexdigest()
            self._repositories.save_risk_decision(
                decision_id=digest,
                signal_id=assessment.signal_id,
                decision=assessment.decision.value,
                audit_codes=list(assessment.audit_codes),
                payload=payload,
                assessed_at=assessment.updated_at,
            )


class PersistentExecutionService(DemoExecutionService):
    """Execution adapter with durable intent-before-side-effect protection."""

    def __init__(
        self,
        risk_service: RiskService,
        settings: Settings,
        private_client: ExecutionPrivateClient | None,
        repositories: TradingStateRepositories,
    ) -> None:
        self._repositories = repositories
        super().__init__(risk_service, settings, private_client)
        self._recover_trades()

    def account(self) -> DemoExecutionAccountResponse:
        result = super().account()
        captured_at = result.updated_at
        for position in result.open_positions:
            payload = _payload(position)
            position_id = f"BINANCE_DEMO:{position.symbol}"
            with self._repositories.persistence.transaction() as session:
                existing = session.get(PositionRow, position_id)
                if existing is None:
                    session.add(
                        PositionRow(
                            position_id=position_id,
                            account_scope="BINANCE_DEMO",
                            symbol=position.symbol,
                            quantity_text=format(position.quantity, "f"),
                            entry_price_text=format(position.entry_price, "f"),
                            payload_json=_json(payload),
                            captured_at=captured_at,
                            updated_at=captured_at,
                        )
                    )
                else:
                    existing.quantity_text = format(position.quantity, "f")
                    existing.entry_price_text = format(position.entry_price, "f")
                    existing.payload_json = _json(payload)
                    existing.captured_at = captured_at
                    existing.updated_at = captured_at
        return result

    def activate(
        self,
        signal_id: str,
        request: DemoExecutionActivateRequest | None = None,
    ) -> DemoTradeRecord:
        client_ids = self._client_order_ids(signal_id)
        intent = self._prepare_intent(
            operation="OPEN",
            subject_id=signal_id,
            signal_id=signal_id,
            client_order_ids=list(client_ids),
            payload={"requested_operation": "OPEN_PROTECTED_DEMO_TRADE"},
        )
        recovered = self._trade_for_signal(signal_id)
        if intent.state in {"COMPLETED", "PROTECTED"} and recovered is not None:
            return recovered
        if intent.state == "RECOVERY_REQUIRED":
            recovery_payload = json.loads(intent.payload_json)
            trade_payload = recovery_payload.get("trade")
            if isinstance(trade_payload, dict):
                trade = DemoTradeRecord.model_validate(trade_payload)
                self._persist_open_result(trade)
                self._trades[trade.trade_id] = trade
                self._update_intent(
                    intent.intent_id,
                    state="PROTECTED",
                    payload={
                        "trade_id": trade.trade_id,
                        "exchange_order_id": trade.exchange_order_id,
                        "stop_order_id": trade.stop_order_id,
                        "take_profit_order_id": trade.take_profit_order_id,
                    },
                )
                return trade

        trade = super().activate(signal_id, request)
        try:
            self._persist_open_result(trade)
        except Exception:
            self._mark_intent_recovery(
                intent.intent_id,
                payload={
                    "reason": "FINAL_PERSISTENCE_FAILED",
                    "exchange_order_id": trade.exchange_order_id,
                    "trade_id": trade.trade_id,
                    "trade": _payload(trade),
                },
            )
            raise
        self._update_intent(
            intent.intent_id,
            state="PROTECTED",
            payload={
                "trade_id": trade.trade_id,
                "exchange_order_id": trade.exchange_order_id,
                "stop_order_id": trade.stop_order_id,
                "take_profit_order_id": trade.take_profit_order_id,
            },
        )
        return trade

    def prepare_close_intent(self, trade: DemoTradeRecord, client_order_id: str) -> None:
        self._prepare_intent(
            operation="CLOSE",
            subject_id=trade.trade_id,
            signal_id=trade.signal_id,
            client_order_ids=[client_order_id],
            payload={"requested_operation": "CLOSE_DEMO_TRADE", "trade_id": trade.trade_id},
        )

    def complete_close_intent(self, trade: DemoTradeRecord, client_order_id: str) -> None:
        intent_id = self._intent_id("CLOSE", trade.trade_id)
        self._update_intent(
            intent_id,
            state="COMPLETED",
            payload={
                "trade_id": trade.trade_id,
                "client_order_id": client_order_id,
                "closed_at": trade.closed_at.isoformat() if trade.closed_at is not None else None,
            },
        )

    def mark_close_recovery(
        self,
        trade: DemoTradeRecord,
        client_order_id: str,
        reason: str,
    ) -> None:
        self._mark_intent_recovery(
            self._intent_id("CLOSE", trade.trade_id),
            payload={
                "trade_id": trade.trade_id,
                "client_order_id": client_order_id,
                "reason": reason,
            },
        )

    def store_trade(self, trade: DemoTradeRecord) -> DemoTradeRecord:
        stored = super().store_trade(trade)
        self._persist_trade(stored)
        return stored

    def _persist_open_result(self, trade: DemoTradeRecord) -> None:
        now = trade.updated_at
        with self._repositories.persistence.transaction() as session:
            self._repositories.save_order(
                order_id=f"entry:{trade.exchange_order_id}",
                signal_id=trade.signal_id,
                trade_id=trade.trade_id,
                client_order_id=trade.client_order_id,
                exchange_order_id=trade.exchange_order_id,
                symbol=trade.symbol,
                status=trade.order_status,
                quantity=trade.executed_quantity,
                average_price=trade.entry_price,
                payload={"source": "verified_entry_result", "status": trade.order_status},
                created_at=trade.opened_at,
                updated_at=now,
                session=session,
            )
            self._repositories.save_fill(
                fill_id=f"aggregate:{trade.exchange_order_id}",
                order_id=f"entry:{trade.exchange_order_id}",
                quantity=trade.executed_quantity,
                price=trade.entry_price,
                payload={"source": "verified_aggregate_fill"},
                filled_at=trade.opened_at,
                session=session,
            )
            for kind, order_id, client_order_id in (
                ("stop", trade.stop_order_id, trade.stop_client_order_id),
                ("take_profit", trade.take_profit_order_id, trade.take_profit_client_order_id),
            ):
                self._repositories.save_order(
                    order_id=f"{kind}:{order_id}",
                    signal_id=trade.signal_id,
                    trade_id=trade.trade_id,
                    client_order_id=client_order_id,
                    exchange_order_id=order_id,
                    symbol=trade.symbol,
                    status="NEW",
                    quantity=trade.executed_quantity,
                    payload={"source": "verified_protective_order", "kind": kind},
                    created_at=trade.opened_at,
                    updated_at=now,
                    session=session,
                )
            self._persist_trade(trade, session=session)

    @staticmethod
    def _intent_id(operation: str, subject_id: str) -> str:
        return hashlib.sha256(f"{operation}:{subject_id}".encode()).hexdigest()

    def _prepare_intent(
        self,
        *,
        operation: str,
        subject_id: str,
        signal_id: str,
        client_order_ids: list[str],
        payload: dict[str, Any],
    ) -> ExecutionIntentRow:
        reject_sensitive_payload(payload)
        intent_id = self._intent_id(operation, subject_id)
        now = datetime.now(UTC)
        with self._repositories.persistence.transaction() as session:
            row = session.get(ExecutionIntentRow, intent_id)
            if row is None:
                row = ExecutionIntentRow(
                    intent_id=intent_id,
                    operation=operation,
                    subject_id=subject_id,
                    signal_id=signal_id,
                    state="PENDING",
                    client_order_ids_json=_json(client_order_ids),
                    payload_json=_json(payload),
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
            elif json.loads(row.client_order_ids_json) != client_order_ids:
                raise RuntimeError("Execution intent client order IDs are immutable")
            return row

    def _update_intent(
        self,
        intent_id: str,
        *,
        state: str,
        payload: dict[str, Any],
    ) -> None:
        reject_sensitive_payload(payload)
        with self._repositories.persistence.transaction() as session:
            row = session.get(ExecutionIntentRow, intent_id)
            if row is None:
                raise RuntimeError("Durable execution intent is missing")
            row.state = state
            row.payload_json = _json(payload)
            row.updated_at = datetime.now(UTC)

    def _mark_intent_recovery(self, intent_id: str, *, payload: dict[str, Any]) -> None:
        try:
            self._update_intent(intent_id, state="RECOVERY_REQUIRED", payload=payload)
        except Exception:
            return

    def _trade_for_signal(self, signal_id: str) -> DemoTradeRecord | None:
        return next(
            (trade for trade in self._trades.values() if trade.signal_id == signal_id),
            None,
        )

    def _persist_trade(
        self,
        trade: DemoTradeRecord,
        *,
        session: Session | None = None,
    ) -> None:
        payload = _payload(trade)
        transaction = (
            self._repositories.persistence.transaction()
            if session is None
            else nullcontext(session)
        )
        with transaction as db:
            row = db.get(TradeRow, trade.trade_id)
            exit_price_text = (
                format(trade.exit_price, "f") if trade.exit_price is not None else None
            )
            if row is None:
                db.add(
                    TradeRow(
                        trade_id=trade.trade_id,
                        signal_id=trade.signal_id,
                        lifecycle=trade.lifecycle.value,
                        symbol=trade.symbol,
                        quantity_text=format(trade.executed_quantity, "f"),
                        entry_price_text=format(trade.entry_price, "f"),
                        exit_price_text=exit_price_text,
                        realized_pnl_text=format(trade.realized_pnl_usdt, "f"),
                        payload_json=_json(payload),
                        opened_at=trade.opened_at,
                        closed_at=trade.closed_at,
                        updated_at=trade.updated_at,
                    )
                )
            else:
                row.lifecycle = trade.lifecycle.value
                row.exit_price_text = exit_price_text
                row.realized_pnl_text = format(trade.realized_pnl_usdt, "f")
                row.payload_json = _json(payload)
                row.closed_at = trade.closed_at
                row.updated_at = trade.updated_at

    def _recover_trades(self) -> None:
        with self._repositories.persistence.transaction() as session:
            rows = list(session.scalars(select(TradeRow).order_by(TradeRow.opened_at)))
        for row in rows:
            trade = DemoTradeRecord.model_validate_json(row.payload_json)
            self._trades[trade.trade_id] = trade
