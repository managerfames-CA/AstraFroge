"""Exchange-authoritative Active Trades projection from Binance Demo positions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.schemas.execution import DemoTradeLifecycle, DemoTradeRecord
from app.schemas.scanner import ScannerDirection, ScannerGrade
from app.schemas.trade_management import (
    ManagedTradeRecordList,
    TradeListFilters,
    TradeManagementState,
    TradeManagementStatusResponse,
    TradeManagementSummary,
    TradeSortBy,
)
from app.services.account_snapshot import (
    AccountPositionSnapshot,
    AccountSnapshot,
    AccountSnapshotPayloadError,
)


class ActiveTradeSnapshotSource(Protocol):
    """Fresh Binance Demo account snapshot source."""

    def force_refresh(self) -> AccountSnapshot: ...


class ActiveTradeHistorySource(Protocol):
    """Durable trade history and execution-status source."""

    def status(self) -> TradeManagementStatusResponse: ...

    def trades(self, filters: TradeListFilters) -> ManagedTradeRecordList: ...


class ActiveTradeAuthorityService:
    """Publish open trades only when current exchange positions prove them."""

    def __init__(
        self,
        trade_source: ActiveTradeHistorySource,
        snapshot_source: ActiveTradeSnapshotSource | None,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._trade_source = trade_source
        self._snapshot_source = snapshot_source
        self._now = now_provider or (lambda: datetime.now(UTC))

    def status(self) -> TradeManagementStatusResponse:
        """Return summary counts derived from exchange-verified open positions."""

        base = self._trade_source.status()
        active = self.trades(TradeListFilters())
        open_trades = active.trades
        state = active.source_state
        if state is TradeManagementState.READY:
            state = base.state
        return TradeManagementStatusResponse(
            state=state,
            contract_version="1",
            exchange_authoritative_active_trades=(
                active.exchange_authoritative_open_trades
            ),
            execution_engine_state=base.execution_engine_state,
            max_open_trades_limit=base.max_open_trades_limit,
            tracked_trade_count=base.tracked_trade_count,
            open_trade_count=len(open_trades),
            available_tracking_slots=max(
                0,
                base.max_open_trades_limit - len(open_trades),
            ),
            local_open_candidate_count=active.local_open_candidate_count,
            exchange_open_position_count=active.exchange_open_position_count,
            rejected_open_trade_count=active.rejected_open_trade_count,
            rejection_codes=active.rejection_codes,
            position_snapshot_id=active.position_snapshot_id,
            position_snapshot_at=active.position_snapshot_at,
            updated_at=active.position_snapshot_at or base.updated_at,
            summary=TradeManagementSummary(
                manual_demo_trades=len(open_trades),
                long_demo=sum(
                    trade.direction is ScannerDirection.LONG for trade in open_trades
                ),
                short_demo=sum(
                    trade.direction is ScannerDirection.SHORT for trade in open_trades
                ),
                combined_unrealized_pnl_usdt=sum(
                    (trade.unrealized_pnl_usdt for trade in open_trades),
                    Decimal("0"),
                ),
                total_tracked_margin_usdt=sum(
                    (trade.tracked_margin_usdt for trade in open_trades),
                    Decimal("0"),
                ),
            ),
        )

    def trades(self, filters: TradeListFilters) -> ManagedTradeRecordList:
        """Return verified open trades and optional durable closed history."""

        durable = self._trade_source.trades(
            TradeListFilters(include_closed=True)
        ).trades
        open_candidates = [
            trade for trade in durable if trade.lifecycle is DemoTradeLifecycle.OPEN
        ]
        closed_records = [
            trade for trade in durable if trade.lifecycle is DemoTradeLifecycle.CLOSED
        ]
        source = self._snapshot_source
        if source is None:
            return self._unavailable(
                filters,
                closed_records,
                open_candidates,
                "DEMO_PRIVATE_API_NOT_CONFIGURED",
            )

        try:
            snapshot = source.force_refresh()
            verified, rejection_codes, exchange_count = self._verify(
                open_candidates,
                snapshot,
            )
        except (BinanceDemoPrivateClientError, AccountSnapshotPayloadError):
            return self._unavailable(
                filters,
                closed_records,
                open_candidates,
                "ACTIVE_TRADES_POSITION_VERIFICATION_UNAVAILABLE",
            )
        except Exception:
            return self._unavailable(
                filters,
                closed_records,
                open_candidates,
                "ACTIVE_TRADES_POSITION_VERIFICATION_INVALID",
            )

        state = (
            TradeManagementState.SOURCE_VERIFICATION_INCOMPLETE
            if rejection_codes
            else TradeManagementState.READY
        )
        if rejection_codes:
            verified = []
        records = [*verified, *(closed_records if filters.include_closed else [])]
        records = self._filter_and_sort(records, filters)
        return ManagedTradeRecordList(
            count=len(records),
            trades=records,
            source_state=state,
            exchange_authoritative_open_trades=not rejection_codes,
            local_open_candidate_count=len(open_candidates),
            exchange_open_position_count=exchange_count,
            rejected_open_trade_count=(len(open_candidates) if rejection_codes else 0),
            rejection_codes=sorted(set(rejection_codes)),
            position_snapshot_id=snapshot.snapshot_id,
            position_snapshot_at=snapshot.captured_at,
        )

    def _verify(
        self,
        open_candidates: list[DemoTradeRecord],
        snapshot: AccountSnapshot,
    ) -> tuple[list[DemoTradeRecord], list[str], int]:
        rejection_codes: list[str] = []
        local_by_symbol: dict[str, DemoTradeRecord] = {}
        for trade in open_candidates:
            if trade.symbol in local_by_symbol:
                rejection_codes.append("ACTIVE_TRADES_DUPLICATE_LOCAL_SYMBOL")
                continue
            local_by_symbol[trade.symbol] = trade

        exchange_by_symbol: dict[str, AccountPositionSnapshot] = {}
        for exchange_position in snapshot.positions:
            if exchange_position.position_amount == 0:
                continue
            if (
                not exchange_position.symbol
                or not exchange_position.position_amount.is_finite()
                or not exchange_position.entry_price.is_finite()
                or exchange_position.entry_price <= 0
                or not exchange_position.unrealized_pnl.is_finite()
            ):
                rejection_codes.append("ACTIVE_TRADES_POSITION_PAYLOAD_INVALID")
                continue
            if exchange_position.symbol in exchange_by_symbol:
                rejection_codes.append("ACTIVE_TRADES_DUPLICATE_EXCHANGE_SYMBOL")
                continue
            exchange_by_symbol[exchange_position.symbol] = exchange_position

        verified: list[DemoTradeRecord] = []
        for symbol, trade in local_by_symbol.items():
            matched_position = exchange_by_symbol.get(symbol)
            if matched_position is None:
                rejection_codes.append("ACTIVE_TRADES_POSITION_MISSING")
                continue
            direction = (
                ScannerDirection.LONG
                if matched_position.position_amount > 0
                else ScannerDirection.SHORT
            )
            quantity = abs(matched_position.position_amount)
            if direction is not trade.direction:
                rejection_codes.append("ACTIVE_TRADES_DIRECTION_MISMATCH")
                continue
            if quantity != trade.executed_quantity:
                rejection_codes.append("ACTIVE_TRADES_QUANTITY_MISMATCH")
                continue
            verified.append(
                trade.model_copy(
                    update={
                        "entry_price": matched_position.entry_price,
                        "executed_quantity": quantity,
                        "unrealized_pnl_usdt": matched_position.unrealized_pnl,
                        "exchange_position_verified": True,
                        "position_snapshot_id": snapshot.snapshot_id,
                        "position_snapshot_at": snapshot.captured_at,
                        "position_source": snapshot.source,
                        "exchange_position_quantity": quantity,
                        "updated_at": snapshot.captured_at,
                    }
                )
            )

        if set(exchange_by_symbol) - set(local_by_symbol):
            rejection_codes.append("ACTIVE_TRADES_ORPHAN_EXCHANGE_POSITION")
        return verified, rejection_codes, len(exchange_by_symbol)

    def _unavailable(
        self,
        filters: TradeListFilters,
        closed_records: list[DemoTradeRecord],
        open_candidates: list[DemoTradeRecord],
        code: str,
    ) -> ManagedTradeRecordList:
        records = closed_records if filters.include_closed else []
        records = self._filter_and_sort(records, filters)
        return ManagedTradeRecordList(
            count=len(records),
            trades=records,
            source_state=TradeManagementState.EXCHANGE_VERIFICATION_UNAVAILABLE,
            exchange_authoritative_open_trades=False,
            local_open_candidate_count=len(open_candidates),
            exchange_open_position_count=0,
            rejected_open_trade_count=len(open_candidates),
            rejection_codes=[code],
        )

    @classmethod
    def _filter_and_sort(
        cls,
        records: list[DemoTradeRecord],
        filters: TradeListFilters,
    ) -> list[DemoTradeRecord]:
        filtered = records
        if filters.symbol is not None:
            filtered = [trade for trade in filtered if trade.symbol == filters.symbol]
        if filters.direction is not None:
            filtered = [
                trade for trade in filtered if trade.direction is filters.direction
            ]
        if filters.min_grade is not None:
            filtered = [
                trade
                for trade in filtered
                if cls._meets_min_grade(trade, filters.min_grade)
            ]
        if filters.sort_by is TradeSortBy.UNREALIZED_PNL_DESC:
            return sorted(
                filtered,
                key=lambda trade: (trade.unrealized_pnl_usdt, trade.opened_at),
                reverse=True,
            )
        if filters.sort_by is TradeSortBy.UNREALIZED_PNL_ASC:
            return sorted(
                filtered,
                key=lambda trade: (trade.unrealized_pnl_usdt, trade.opened_at),
            )
        return sorted(filtered, key=lambda trade: trade.opened_at, reverse=True)

    @staticmethod
    def _meets_min_grade(trade: DemoTradeRecord, minimum: ScannerGrade) -> bool:
        ranking = {"A+": 3, "A": 2, "B+": 1, "Reject": 0}
        trade_rank = ranking.get(trade.grade.value, -1) if trade.grade else -1
        return trade_rank >= ranking[minimum.value]
