"""Exchange-authoritative Demo trade close and active-trades management."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.core.errors import AppError
from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.schemas.execution import (
    DemoExecutionState,
    DemoTradeCloseReason,
    DemoTradeLifecycle,
    DemoTradeRecord,
)
from app.schemas.scanner import ScannerDirection, ScannerGrade
from app.schemas.trade_management import (
    ManagedTradeRecordList,
    TradeCloseRequest,
    TradeListFilters,
    TradeManagementState,
    TradeManagementStatusResponse,
    TradeManagementSummary,
    TradeSortBy,
)
from app.services.execution import DemoExecutionService

_ORDER_NOT_FOUND = -2013


class TradeClosePrivateClient(Protocol):
    """Binance Demo methods required for a verified operator close."""

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]: ...

    def cancel_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]: ...

    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str,
        new_client_order_id: str,
        reduce_only: bool = False,
    ) -> dict[str, Any]: ...

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]: ...


class TradeManagementService:
    """Summarize, filter, and close Demo trades using verified exchange fills."""

    def __init__(
        self,
        execution_service: DemoExecutionService,
        private_client: TradeClosePrivateClient | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._execution = execution_service
        self._private_client = private_client
        self._now = now_provider or (lambda: datetime.now(UTC))

    def status(self) -> TradeManagementStatusResponse:
        execution_status = self._execution.status()
        trades = self.trades(TradeListFilters(include_closed=True)).trades
        open_trades = [trade for trade in trades if trade.lifecycle is DemoTradeLifecycle.OPEN]
        updated_at = max(
            (trade.updated_at for trade in trades),
            default=execution_status.updated_at,
        )
        summary = TradeManagementSummary(
            manual_demo_trades=len(open_trades),
            long_demo=sum(trade.direction is ScannerDirection.LONG for trade in open_trades),
            short_demo=sum(trade.direction is ScannerDirection.SHORT for trade in open_trades),
            combined_unrealized_pnl_usdt=sum(
                (trade.unrealized_pnl_usdt for trade in open_trades),
                Decimal("0"),
            ),
            total_tracked_margin_usdt=sum(
                (trade.tracked_margin_usdt for trade in open_trades),
                Decimal("0"),
            ),
        )
        return TradeManagementStatusResponse(
            state=self._state_from_execution(execution_status.state),
            contract_version="1",
            execution_engine_state=execution_status.state.value,
            max_open_trades_limit=execution_status.max_open_trades_limit,
            tracked_trade_count=len(trades),
            open_trade_count=len(open_trades),
            available_tracking_slots=max(
                0,
                execution_status.max_open_trades_limit - len(open_trades),
            ),
            updated_at=updated_at,
            summary=summary,
        )

    def trades(self, filters: TradeListFilters) -> ManagedTradeRecordList:
        trades = self._execution.trades().trades
        if not filters.include_closed:
            trades = [trade for trade in trades if trade.lifecycle is DemoTradeLifecycle.OPEN]
        if filters.symbol is not None:
            trades = [trade for trade in trades if trade.symbol == filters.symbol]
        if filters.direction is not None:
            trades = [trade for trade in trades if trade.direction is filters.direction]
        if filters.min_grade is not None:
            trades = [trade for trade in trades if self._meets_min_grade(trade, filters.min_grade)]

        if filters.sort_by is TradeSortBy.UNREALIZED_PNL_DESC:
            trades = sorted(
                trades,
                key=lambda trade: (trade.unrealized_pnl_usdt, trade.opened_at),
                reverse=True,
            )
        elif filters.sort_by is TradeSortBy.UNREALIZED_PNL_ASC:
            trades = sorted(
                trades,
                key=lambda trade: (trade.unrealized_pnl_usdt, trade.opened_at),
            )
        else:
            trades = sorted(trades, key=lambda trade: trade.opened_at, reverse=True)

        return ManagedTradeRecordList(count=len(trades), trades=trades)

    def close_trade(self, trade_id: str, request: TradeCloseRequest) -> DemoTradeRecord:
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
        if trade.lifecycle is DemoTradeLifecycle.CLOSED:
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

        close_client_id = f"af-m-{trade.signal_id[:20]}"
        prepare = getattr(self._execution, "prepare_close_intent", None)
        if callable(prepare):
            prepare(trade, close_client_id)

        exchange_succeeded = False
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

            exchange_succeeded = True
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
            closed = self._execution.store_trade(
                trade.model_copy(
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
            )
            complete = getattr(self._execution, "complete_close_intent", None)
            if callable(complete):
                complete(closed, close_client_id)
            return closed
        except Exception:
            if exchange_succeeded:
                recover = getattr(self._execution, "mark_close_recovery", None)
                if callable(recover):
                    recover(trade, close_client_id, "CLOSE_FINALIZATION_FAILED")
            raise

    @staticmethod
    def _existing_or_new_close(
        *,
        client: TradeClosePrivateClient,
        trade: DemoTradeRecord,
        client_order_id: str,
    ) -> dict[str, Any]:
        try:
            return client.query_order(
                symbol=trade.symbol,
                orig_client_order_id=client_order_id,
            )
        except BinanceDemoPrivateClientError as exc:
            if exc.exchange_code != _ORDER_NOT_FOUND:
                raise
        side = "SELL" if trade.direction is ScannerDirection.LONG else "BUY"
        try:
            return client.place_market_order(
                symbol=trade.symbol,
                side=side,
                quantity=format(trade.executed_quantity, "f"),
                new_client_order_id=client_order_id,
                reduce_only=True,
            )
        except BinanceDemoPrivateClientError as submit_error:
            try:
                return client.query_order(
                    symbol=trade.symbol,
                    orig_client_order_id=client_order_id,
                )
            except BinanceDemoPrivateClientError as query_error:
                if query_error.exchange_code == _ORDER_NOT_FOUND:
                    raise submit_error from query_error
                raise

    @staticmethod
    def _verified_close_fill(
        *,
        payload: dict[str, Any],
        trade: DemoTradeRecord,
        expected_client_order_id: str,
    ) -> tuple[Decimal, Decimal]:
        if (
            payload.get("clientOrderId") != expected_client_order_id
            or payload.get("orderId") is None
        ):
            raise AppError(
                status_code=502,
                code="TRADE_CLOSE_IDENTITY_INVALID",
                message="Binance Demo did not confirm the expected close order identity",
            )
        if payload.get("status") != "FILLED":
            raise AppError(
                status_code=502,
                code="TRADE_CLOSE_NOT_VERIFIED",
                message="Binance Demo close order is not fully filled",
            )
        exit_price = TradeManagementService._positive_decimal(
            payload.get("avgPrice"),
            field="avgPrice",
        )
        executed_quantity = TradeManagementService._positive_decimal(
            payload.get("executedQty"),
            field="executedQty",
        )
        if executed_quantity != trade.executed_quantity:
            raise AppError(
                status_code=502,
                code="TRADE_CLOSE_QUANTITY_MISMATCH",
                message="Binance Demo did not close the full tracked position quantity",
            )
        return exit_price, executed_quantity

    @staticmethod
    def _positive_decimal(value: Any, *, field: str) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise AppError(
                status_code=502,
                code="DEMO_EXCHANGE_PAYLOAD_INVALID",
                message=f"Binance Demo field {field} is invalid",
            ) from exc
        if not parsed.is_finite() or parsed <= 0:
            raise AppError(
                status_code=502,
                code="DEMO_EXCHANGE_PAYLOAD_INVALID",
                message=f"Binance Demo field {field} must be positive",
            )
        return parsed

    @staticmethod
    def _gross_realized_pnl(
        *,
        direction: ScannerDirection,
        entry_price: Decimal,
        exit_price: Decimal,
        quantity: Decimal,
    ) -> Decimal:
        price_change = (
            exit_price - entry_price
            if direction is ScannerDirection.LONG
            else entry_price - exit_price
        )
        return price_change * quantity

    def _reconcile_realized_pnl(
        self,
        *,
        client: TradeClosePrivateClient,
        trade: DemoTradeRecord,
        closed_at: datetime,
    ) -> dict[str, Decimal]:
        start_time_ms = int((trade.opened_at - timedelta(minutes=5)).timestamp() * 1000)
        end_time_ms = int((closed_at + timedelta(minutes=5)).timestamp() * 1000)
        try:
            income_rows = client.income_history(
                start_time_ms=start_time_ms,
                end_time_ms=end_time_ms,
                limit=1000,
            )
        except BinanceDemoPrivateClientError as exc:
            raise AppError(
                status_code=502,
                code="DEMO_INCOME_RECONCILIATION_FAILED",
                message=(
                    "Binance Demo income records are unavailable; the close remains "
                    "recovery-required"
                ),
            ) from exc

        realized = Decimal("0")
        commission = Decimal("0")
        funding = Decimal("0")
        realized_verified = False
        for item in income_rows:
            if str(item.get("symbol", "")) != trade.symbol:
                continue
            income_type = item.get("incomeType")
            if income_type not in {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}:
                continue
            amount = self._income_decimal(item.get("income"))
            if amount is None:
                raise AppError(
                    status_code=502,
                    code="DEMO_INCOME_PAYLOAD_INVALID",
                    message="Binance Demo returned an invalid income amount",
                )
            if income_type == "REALIZED_PNL":
                realized += amount
                realized_verified = True
            elif income_type == "COMMISSION":
                commission += amount
            else:
                funding += amount

        if not realized_verified:
            raise AppError(
                status_code=502,
                code="DEMO_REALIZED_PNL_UNVERIFIED",
                message=(
                    "Binance Demo did not return a verified realized-PnL income record; "
                    "the close remains recovery-required"
                ),
            )
        return {
            "realized_pnl_usdt": realized + commission + funding,
            "commission_usdt": commission,
            "funding_fees_usdt": funding,
        }

    @staticmethod
    def _income_decimal(value: Any) -> Decimal | None:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not parsed.is_finite():
            return None
        return parsed

    @staticmethod
    def _cancel_best_effort(
        client: TradeClosePrivateClient,
        *,
        symbol: str,
        client_order_id: str,
    ) -> None:
        try:
            client.cancel_order(
                symbol=symbol,
                orig_client_order_id=client_order_id,
            )
        except BinanceDemoPrivateClientError:
            return

    @staticmethod
    def _state_from_execution(execution_state: DemoExecutionState) -> TradeManagementState:
        if execution_state is DemoExecutionState.READY:
            return TradeManagementState.READY
        return TradeManagementState.WAITING_FOR_EXECUTION

    @staticmethod
    def _meets_min_grade(trade: DemoTradeRecord, minimum: ScannerGrade) -> bool:
        ranking = {
            ScannerGrade.A_PLUS: 3,
            ScannerGrade.A: 2,
            ScannerGrade.B_PLUS: 1,
            ScannerGrade.REJECT: 0,
        }
        trade_rank = ranking.get(trade.grade, -1) if trade.grade is not None else -1
        return trade_rank >= ranking[minimum]
