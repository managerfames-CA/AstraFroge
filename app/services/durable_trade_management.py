"""Durable and idempotent manual-close orchestration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from app.core.errors import AppError
from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.schemas.execution import DemoTradeCloseReason, DemoTradeLifecycle, DemoTradeRecord
from app.schemas.trade_management import (
    ManualCloseIntentSnapshot,
    ManualCloseIntentState,
    TradeCloseRequest,
)
from app.services.execution import DemoExecutionService
from app.services.manual_close_durability import (
    ManualCloseDurabilityError,
    ManualCloseDurabilityService,
)
from app.services.trade_management import TradeClosePrivateClient, TradeManagementService


class DurableTradeManagementService(TradeManagementService):
    """Require durable intent and deterministic replay for every manual close."""

    def __init__(
        self,
        execution_service: DemoExecutionService,
        private_client: TradeClosePrivateClient | None,
        close_durability: ManualCloseDurabilityService | None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(execution_service, private_client, now_provider)
        self._close_durability = close_durability

    def close_trade(self, trade_id: str, request: TradeCloseRequest) -> DemoTradeRecord:
        """Close once, recover by deterministic identity, and replay one final outcome."""

        if request.exit_price is not None or request.realized_pnl_usdt is not None:
            raise AppError(
                status_code=409,
                code="CLIENT_CLOSE_VALUES_NOT_ALLOWED",
                message="Exit price and realized PnL must come from the Binance Demo fill",
            )
        trade = self._execution.get_trade(trade_id)
        if trade is None:
            raise AppError(
                status_code=404,
                code="TRADE_NOT_FOUND",
                message="No tracked demo trade was found",
            )

        durability = self._close_durability
        if durability is None:
            raise AppError(
                status_code=503,
                code="MANUAL_CLOSE_DURABILITY_UNAVAILABLE",
                message="Durable persistence is required before a manual close",
            )
        close_client_id = f"af-m-{trade.signal_id[:20]}"
        try:
            intent = durability.prepare(trade, close_client_id)
        except ManualCloseDurabilityError as exc:
            raise AppError(
                status_code=503,
                code="MANUAL_CLOSE_DURABILITY_FAILED",
                message=str(exc),
            ) from exc

        replayed = self._completed_trade(intent)
        if replayed is not None:
            self._execution.store_trade(replayed)
            return replayed

        if trade.lifecycle is DemoTradeLifecycle.CLOSED:
            if intent.state in {
                ManualCloseIntentState.FILLED,
                ManualCloseIntentState.RECOVERY_REQUIRED,
            }:
                try:
                    completed_intent = durability.complete(trade, close_client_id)
                except ManualCloseDurabilityError as exc:
                    raise AppError(
                        status_code=503,
                        code="MANUAL_CLOSE_DURABILITY_FAILED",
                        message=str(exc),
                    ) from exc
                authoritative = self._require_completed_trade(completed_intent)
                self._execution.store_trade(authoritative)
                return authoritative
            raise AppError(
                status_code=409,
                code="TRADE_ALREADY_CLOSED",
                message="Tracked demo trade is already closed",
            )

        client = self._private_client
        if client is None:
            raise AppError(
                status_code=409,
                code="DEMO_PRIVATE_API_NOT_CONFIGURED",
                message="Binance Demo private API is required to close a trade",
            )

        exchange_observed = False
        try:
            try:
                payload = self._existing_or_new_close(
                    client=client,
                    trade=trade,
                    client_order_id=close_client_id,
                )
            except BinanceDemoPrivateClientError as exc:
                raise AppError(
                    status_code=502,
                    code="DEMO_CLOSE_ORDER_REJECTED",
                    message=str(exc),
                ) from exc

            exchange_observed = True
            try:
                recorded = durability.record_exchange_result(
                    trade,
                    close_client_id,
                    payload,
                )
            except ManualCloseDurabilityError as exc:
                raise AppError(
                    status_code=503,
                    code="MANUAL_CLOSE_EXCHANGE_STATE_PERSIST_FAILED",
                    message=str(exc),
                ) from exc
            replayed = self._completed_trade(recorded)
            if replayed is not None:
                self._execution.store_trade(replayed)
                return replayed

            exit_price, executed_quantity = self._verified_close_fill(
                payload=payload,
                trade=trade,
                expected_client_order_id=close_client_id,
            )
            self._cancel_best_effort(
                client,
                symbol=trade.symbol,
                client_order_id=trade.stop_client_order_id,
            )
            self._cancel_best_effort(
                client,
                symbol=trade.symbol,
                client_order_id=trade.take_profit_client_order_id,
            )

            now = self._now()
            gross_realized_pnl = self._gross_realized_pnl(
                direction=trade.direction,
                entry_price=trade.entry_price,
                exit_price=exit_price,
                quantity=executed_quantity,
            )
            reconciled_pnl = self._reconcile_realized_pnl(
                client=client,
                trade=trade,
                closed_at=now,
            )
            provisional = trade.model_copy(
                update={
                    "lifecycle": DemoTradeLifecycle.CLOSED,
                    "exit_price": exit_price,
                    "realized_pnl_usdt": reconciled_pnl["realized_pnl_usdt"],
                    "gross_realized_pnl_usdt": gross_realized_pnl,
                    "commission_usdt": reconciled_pnl["commission_usdt"],
                    "funding_fees_usdt": reconciled_pnl["funding_fees_usdt"],
                    "order_status": "FILLED",
                    "closed_at": now,
                    "closed_reason": DemoTradeCloseReason(request.reason.value),
                    "updated_at": now,
                }
            )
            try:
                completed_intent = durability.complete(provisional, close_client_id)
            except ManualCloseDurabilityError as exc:
                raise AppError(
                    status_code=503,
                    code="MANUAL_CLOSE_COMPLETION_PERSIST_FAILED",
                    message=str(exc),
                ) from exc
            authoritative = self._require_completed_trade(completed_intent)
            self._execution.store_trade(authoritative)
            return authoritative
        except Exception:
            if exchange_observed:
                try:
                    durability.mark_recovery_required(
                        trade,
                        close_client_id,
                        "CLOSE_FINALIZATION_FAILED",
                    )
                except ManualCloseDurabilityError:
                    pass
            raise

    @staticmethod
    def _completed_trade(
        intent: ManualCloseIntentSnapshot,
    ) -> DemoTradeRecord | None:
        if intent.state is not ManualCloseIntentState.COMPLETED:
            return None
        completed = intent.completed_trade
        if completed is None:
            raise AppError(
                status_code=503,
                code="MANUAL_CLOSE_COMPLETION_INVALID",
                message="Durable manual-close completion has no authoritative trade",
            )
        return completed

    @classmethod
    def _require_completed_trade(
        cls,
        intent: ManualCloseIntentSnapshot,
    ) -> DemoTradeRecord:
        completed = cls._completed_trade(intent)
        if completed is None:
            raise AppError(
                status_code=503,
                code="MANUAL_CLOSE_COMPLETION_INVALID",
                message="Durable manual-close completion did not reach COMPLETED",
            )
        return completed
