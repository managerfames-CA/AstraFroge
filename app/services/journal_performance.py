"""Exchange-verified closed-trade Journal and performance reporting."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

from app.integrations.binance.private_demo_client import (
    BinanceDemoPrivateClientError,
)
from app.schemas.execution import DemoTradeLifecycle, DemoTradeRecord
from app.schemas.journal_performance import (
    JournalEntry,
    JournalEntryList,
    JournalFilters,
    JournalPerformanceState,
    JournalPerformanceStatusResponse,
    JournalPerformanceSummary,
    JournalPnlSource,
    JournalSortBy,
    PerformanceSnapshotResponse,
)
from app.schemas.scanner import ScannerGrade
from app.schemas.trade_management import TradeListFilters, TradeManagementState
from app.services.journal_cost_verification import (
    JournalActualCostEvidence,
    JournalCostClient,
    JournalCostVerificationService,
)
from app.services.journal_exchange_verification import (
    JournalExchangeClient,
    JournalExchangeEvidence,
    JournalExchangeVerificationService,
    JournalSourceVerificationError,
)
from app.services.trade_management import TradeManagementService


@dataclass(frozen=True)
class _VerifiedTrade:
    trade: DemoTradeRecord
    evidence: JournalExchangeEvidence
    costs: JournalActualCostEvidence


@dataclass(frozen=True)
class _VerificationSnapshot:
    candidate_count: int
    records: tuple[_VerifiedTrade, ...]
    rejected_count: int
    rejection_codes: tuple[str, ...]


class JournalPerformanceService:
    """Project only exchange-verified closed Demo trades into Journal views."""

    def __init__(
        self,
        trade_management_service: TradeManagementService,
        verification_client: JournalExchangeClient | None = None,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._trade_management = trade_management_service
        inferred_client: Any = verification_client
        if inferred_client is None:
            adapter = getattr(trade_management_service, "_private_client", None)
            inferred_client = getattr(adapter, "_client", adapter)
        self._now = now_provider or (lambda: datetime.now(UTC))
        self._verification = JournalExchangeVerificationService(
            cast(JournalExchangeClient | None, inferred_client),
            now_provider=self._now,
        )
        self._cost_verification = JournalCostVerificationService(
            cast(JournalCostClient | None, inferred_client),
            now_provider=self._now,
        )

    def status(self) -> JournalPerformanceStatusResponse:
        trade_management_status = self._trade_management.status()
        snapshot = self._verification_snapshot()
        performance = self._performance_from_snapshot(snapshot, lookback_days=30)
        latest_closed_trade_at = max(
            (
                record.trade.closed_at
                for record in snapshot.records
                if record.trade.closed_at is not None
            ),
            default=None,
        )
        updated_at = max(
            (record.trade.updated_at for record in snapshot.records),
            default=trade_management_status.updated_at,
        )
        return JournalPerformanceStatusResponse(
            state=self._source_state(trade_management_status.state, snapshot),
            contract_version="1",
            trade_management_state=trade_management_status.state,
            lookback_days=performance.lookback_days,
            latest_closed_trade_at=latest_closed_trade_at,
            updated_at=updated_at,
            candidate_count=snapshot.candidate_count,
            verified_count=len(snapshot.records),
            rejected_count=snapshot.rejected_count,
            rejection_codes=list(snapshot.rejection_codes),
            summary=performance.summary,
        )

    def journal(self, filters: JournalFilters) -> JournalEntryList:
        trade_management_state = self._trade_management.status().state
        snapshot = self._verification_snapshot()
        records = list(snapshot.records)
        if filters.symbol is not None:
            records = [
                item for item in records if item.trade.symbol == filters.symbol
            ]
        if filters.direction is not None:
            records = [
                item for item in records if item.trade.direction is filters.direction
            ]
        if filters.min_grade is not None:
            records = [
                item
                for item in records
                if self._meets_min_grade(item.trade, filters.min_grade)
            ]
        if filters.close_reason is not None:
            records = [
                item
                for item in records
                if item.trade.closed_reason is filters.close_reason
            ]

        if filters.sort_by is JournalSortBy.REALIZED_PNL_DESC:
            records = sorted(
                records,
                key=lambda item: (
                    item.costs.net_realized_pnl_usdt,
                    item.trade.closed_at or item.trade.updated_at,
                ),
                reverse=True,
            )
        elif filters.sort_by is JournalSortBy.REALIZED_PNL_ASC:
            records = sorted(
                records,
                key=lambda item: (
                    item.costs.net_realized_pnl_usdt,
                    item.trade.closed_at or item.trade.updated_at,
                ),
            )
        else:
            records = sorted(
                records,
                key=lambda item: item.trade.closed_at or item.trade.updated_at,
                reverse=True,
            )

        entries = [self._to_journal_entry(item) for item in records]
        return JournalEntryList(
            count=len(entries),
            entries=entries,
            source_state=self._source_state(trade_management_state, snapshot),
            candidate_count=snapshot.candidate_count,
            rejected_count=snapshot.rejected_count,
            rejection_codes=list(snapshot.rejection_codes),
        )

    def performance(self, lookback_days: int = 30) -> PerformanceSnapshotResponse:
        snapshot = self._verification_snapshot()
        return self._performance_from_snapshot(
            snapshot,
            lookback_days=lookback_days,
        )

    def _performance_from_snapshot(
        self,
        snapshot: _VerificationSnapshot,
        *,
        lookback_days: int,
    ) -> PerformanceSnapshotResponse:
        now = self._now()
        window_started_at = now - timedelta(days=lookback_days)
        window_records = [
            record
            for record in snapshot.records
            if record.trade.closed_at is not None
            and record.trade.closed_at >= window_started_at
        ]
        winning = [
            record
            for record in window_records
            if record.costs.net_realized_pnl_usdt > 0
        ]
        losing = [
            record
            for record in window_records
            if record.costs.net_realized_pnl_usdt < 0
        ]
        breakeven = [
            record
            for record in window_records
            if record.costs.net_realized_pnl_usdt == 0
        ]
        closed_count = len(window_records)
        realized_pnl = sum(
            (record.costs.net_realized_pnl_usdt for record in window_records),
            Decimal("0"),
        )
        gross_realized_pnl = sum(
            (record.evidence.gross_realized_pnl_usdt for record in window_records),
            Decimal("0"),
        )
        win_rate = (
            (
                Decimal(len(winning))
                / Decimal(closed_count)
                * Decimal("100")
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
            if closed_count
            else Decimal("0")
        )
        average_win = (
            sum(
                (record.costs.net_realized_pnl_usdt for record in winning),
                Decimal("0"),
            )
            / Decimal(len(winning))
            if winning
            else None
        )
        average_loss = (
            sum(
                (record.costs.net_realized_pnl_usdt for record in losing),
                Decimal("0"),
            )
            / Decimal(len(losing))
            if losing
            else None
        )
        summary = JournalPerformanceSummary(
            closed_trade_count=closed_count,
            winning_trades=len(winning),
            losing_trades=len(losing),
            breakeven_trades=len(breakeven),
            realized_pnl_usdt=realized_pnl,
            gross_realized_pnl_usdt=gross_realized_pnl,
            pnl_source=JournalPnlSource.VERIFIED_FILLS_NET_ACTUAL_COSTS,
            commission_usdt=sum(
                (record.costs.commission_usdt for record in window_records),
                Decimal("0"),
            ),
            funding_fees_usdt=sum(
                (record.costs.funding_usdt for record in window_records),
                Decimal("0"),
            ),
            win_rate_percent=win_rate,
            average_win_usdt=average_win,
            average_loss_usdt=average_loss,
            best_trade_pnl_usdt=max(
                (record.costs.net_realized_pnl_usdt for record in window_records),
                default=None,
            ),
            worst_trade_pnl_usdt=min(
                (record.costs.net_realized_pnl_usdt for record in window_records),
                default=None,
            ),
        )
        state = self._source_state(
            self._trade_management.status().state,
            snapshot,
        )
        return PerformanceSnapshotResponse(
            lookback_days=lookback_days,
            window_started_at=window_started_at,
            window_ended_at=now,
            source_state=state,
            candidate_count=snapshot.candidate_count,
            verified_count=len(snapshot.records),
            rejected_count=snapshot.rejected_count,
            rejection_codes=list(snapshot.rejection_codes),
            summary=summary,
        )

    def _verification_snapshot(self) -> _VerificationSnapshot:
        candidates = self._candidate_closed_trades()
        records: list[_VerifiedTrade] = []
        rejection_codes: list[str] = []
        rejected_count = 0
        for trade in candidates:
            try:
                evidence = self._verification.verify(trade)
                costs = self._cost_verification.verify(trade, evidence)
            except JournalSourceVerificationError as exc:
                rejected_count += 1
                rejection_codes.append(exc.code)
            except BinanceDemoPrivateClientError:
                rejected_count += 1
                rejection_codes.append(
                    "JOURNAL_EXCHANGE_VERIFICATION_UNAVAILABLE"
                )
            except Exception:
                rejected_count += 1
                rejection_codes.append("JOURNAL_EXCHANGE_VERIFICATION_INVALID")
            else:
                records.append(
                    _VerifiedTrade(
                        trade=trade,
                        evidence=evidence,
                        costs=costs,
                    )
                )

        records, ambiguous_count = self._remove_ambiguous_income_records(records)
        if ambiguous_count:
            rejected_count += ambiguous_count
            rejection_codes.append("JOURNAL_INCOME_ATTRIBUTION_AMBIGUOUS")
        return _VerificationSnapshot(
            candidate_count=len(candidates),
            records=tuple(records),
            rejected_count=rejected_count,
            rejection_codes=tuple(sorted(set(rejection_codes))),
        )

    @staticmethod
    def _remove_ambiguous_income_records(
        records: list[_VerifiedTrade],
    ) -> tuple[list[_VerifiedTrade], int]:
        counts = Counter(
            transaction_id
            for record in records
            for transaction_id in record.costs.income_transaction_ids
        )
        ambiguous_ids = {
            transaction_id
            for transaction_id, count in counts.items()
            if count > 1
        }
        if not ambiguous_ids:
            return records, 0
        accepted = [
            record
            for record in records
            if ambiguous_ids.isdisjoint(record.costs.income_transaction_ids)
        ]
        return accepted, len(records) - len(accepted)

    def _candidate_closed_trades(self) -> list[DemoTradeRecord]:
        return [
            trade
            for trade in self._trade_management.trades(
                TradeListFilters(include_closed=True)
            ).trades
            if trade.lifecycle is DemoTradeLifecycle.CLOSED
            and trade.closed_at is not None
        ]

    @staticmethod
    def _source_state(
        trade_management_state: TradeManagementState,
        snapshot: _VerificationSnapshot,
    ) -> JournalPerformanceState:
        if trade_management_state is TradeManagementState.WAITING_FOR_EXECUTION:
            return JournalPerformanceState.WAITING_FOR_TRADE_MANAGEMENT
        if snapshot.candidate_count == 0 or snapshot.rejected_count == 0:
            return JournalPerformanceState.READY
        unavailable_codes = {
            "DEMO_PRIVATE_API_NOT_CONFIGURED",
            "JOURNAL_EXCHANGE_VERIFICATION_UNAVAILABLE",
        }
        if len(snapshot.records) == 0 and unavailable_codes.intersection(
            snapshot.rejection_codes
        ):
            return JournalPerformanceState.EXCHANGE_VERIFICATION_UNAVAILABLE
        return JournalPerformanceState.SOURCE_VERIFICATION_INCOMPLETE

    @staticmethod
    def _to_journal_entry(record: _VerifiedTrade) -> JournalEntry:
        trade = record.trade
        evidence = record.evidence
        costs = record.costs
        if trade.closed_at is None or trade.closed_reason is None:
            raise RuntimeError("Verified Journal record lost closed-trade identity")
        hold_minutes = max(
            0,
            int((trade.closed_at - trade.opened_at).total_seconds() // 60),
        )
        return JournalEntry(
            trade_id=trade.trade_id,
            signal_id=trade.signal_id,
            symbol=trade.symbol,
            direction=trade.direction,
            setup=trade.setup,
            setup_name=trade.setup_name,
            grade=trade.grade,
            entry_price=evidence.entry_average_price,
            exit_price=evidence.close_average_price,
            verified_fill_quantity=evidence.entry_fill_quantity,
            realized_pnl_usdt=costs.net_realized_pnl_usdt,
            gross_realized_pnl_usdt=evidence.gross_realized_pnl_usdt,
            pnl_source=JournalPnlSource.VERIFIED_FILLS_NET_ACTUAL_COSTS,
            tracked_margin_usdt=trade.tracked_margin_usdt,
            commission_usdt=costs.commission_usdt,
            funding_fees_usdt=costs.funding_usdt,
            actual_costs_verified=True,
            opened_at=trade.opened_at,
            closed_at=trade.closed_at,
            closed_reason=trade.closed_reason,
            hold_minutes=hold_minutes,
            source_verified=True,
            source_checked_at=max(evidence.checked_at, costs.checked_at),
            entry_exchange_order_id=evidence.entry_exchange_order_id,
            close_exchange_order_id=evidence.close_exchange_order_id,
            entry_fill_ids=list(evidence.entry_fill_ids),
            close_fill_ids=list(evidence.close_fill_ids),
            income_transaction_ids=list(costs.income_transaction_ids),
            commission_transaction_ids=list(costs.commission_transaction_ids),
            funding_transaction_ids=list(costs.funding_transaction_ids),
        )

    @staticmethod
    def _meets_min_grade(
        trade: DemoTradeRecord,
        minimum: ScannerGrade,
    ) -> bool:
        ranking = {
            "A+": 3,
            "A": 2,
            "B+": 1,
            "Reject": 0,
        }
        trade_rank = (
            ranking.get(trade.grade.value, -1)
            if trade.grade is not None
            else -1
        )
        return trade_rank >= ranking[minimum.value]
