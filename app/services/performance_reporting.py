"""BE-15 strategy, symbol and calendar performance reporting."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.schemas.journal_performance import JournalEntry, JournalFilters
from app.schemas.performance_reporting import (
    PerformanceDimension,
    PerformanceDimensionReport,
    PerformanceReportRow,
    VerifiedPerformanceReportResponse,
)
from app.services.journal_performance import JournalPerformanceService


class VerifiedPerformanceReportingService:
    """Aggregate only exchange-verified Journal entries into BE-15 reports."""

    def __init__(
        self,
        journal_service: JournalPerformanceService,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._journal = journal_service
        self._now = now_provider or (lambda: datetime.now(UTC))

    def report(self, *, lookback_days: int = 30) -> VerifiedPerformanceReportResponse:
        if lookback_days < 1 or lookback_days > 3650:
            raise ValueError("lookback_days must be between 1 and 3650")

        now = self._now()
        window_started_at = now - timedelta(days=lookback_days)
        journal = self._journal.journal(JournalFilters())
        entries = [
            entry
            for entry in journal.entries
            if entry.source_verified
            and entry.actual_costs_verified
            and entry.closed_at >= window_started_at
            and entry.closed_at <= now
        ]

        return VerifiedPerformanceReportResponse(
            generated_at=now,
            window_started_at=window_started_at,
            window_ended_at=now,
            lookback_days=lookback_days,
            source_state=journal.source_state,
            candidate_count=journal.candidate_count,
            verified_count=len(entries),
            rejected_count=journal.rejected_count,
            rejection_codes=list(journal.rejection_codes),
            strategy=self._dimension_report(
                PerformanceDimension.STRATEGY,
                entries,
                key_provider=lambda item: item.setup.value,
                label_provider=lambda item: item.setup_name,
            ),
            symbol=self._dimension_report(
                PerformanceDimension.SYMBOL,
                entries,
                key_provider=lambda item: item.symbol,
                label_provider=lambda item: item.symbol,
            ),
            daily=self._period_report(PerformanceDimension.DAILY, entries),
            weekly=self._period_report(PerformanceDimension.WEEKLY, entries),
            monthly=self._period_report(PerformanceDimension.MONTHLY, entries),
        )

    def _dimension_report(
        self,
        dimension: PerformanceDimension,
        entries: Iterable[JournalEntry],
        *,
        key_provider: Callable[[JournalEntry], str],
        label_provider: Callable[[JournalEntry], str],
    ) -> PerformanceDimensionReport:
        buckets: dict[str, list[JournalEntry]] = defaultdict(list)
        labels: dict[str, str] = {}
        for entry in entries:
            key = key_provider(entry)
            buckets[key].append(entry)
            labels.setdefault(key, label_provider(entry))
        rows = [
            self._row(dimension, key, labels[key], bucket)
            for key, bucket in sorted(buckets.items())
        ]
        return PerformanceDimensionReport(dimension=dimension, count=len(rows), rows=rows)

    def _period_report(
        self,
        dimension: PerformanceDimension,
        entries: Iterable[JournalEntry],
    ) -> PerformanceDimensionReport:
        buckets: dict[date, list[JournalEntry]] = defaultdict(list)
        for entry in entries:
            closed_date = entry.closed_at.astimezone(UTC).date()
            if dimension is PerformanceDimension.DAILY:
                start = closed_date
            elif dimension is PerformanceDimension.WEEKLY:
                start = closed_date - timedelta(days=closed_date.weekday())
            elif dimension is PerformanceDimension.MONTHLY:
                start = closed_date.replace(day=1)
            else:
                raise ValueError(f"Unsupported period dimension: {dimension}")
            buckets[start].append(entry)

        rows: list[PerformanceReportRow] = []
        for start, bucket in sorted(buckets.items(), reverse=True):
            if dimension is PerformanceDimension.DAILY:
                end = start
                label = start.isoformat()
            elif dimension is PerformanceDimension.WEEKLY:
                end = start + timedelta(days=6)
                label = f"{start.isoformat()} to {end.isoformat()}"
            else:
                next_month = (
                    start.replace(year=start.year + 1, month=1)
                    if start.month == 12
                    else start.replace(month=start.month + 1)
                )
                end = next_month - timedelta(days=1)
                label = start.strftime("%Y-%m")
            rows.append(
                self._row(
                    dimension,
                    start.isoformat(),
                    label,
                    bucket,
                    period_started_on=start,
                    period_ended_on=end,
                )
            )
        return PerformanceDimensionReport(dimension=dimension, count=len(rows), rows=rows)

    @staticmethod
    def _row(
        dimension: PerformanceDimension,
        key: str,
        label: str,
        entries: list[JournalEntry],
        *,
        period_started_on: date | None = None,
        period_ended_on: date | None = None,
    ) -> PerformanceReportRow:
        pnl_values = [entry.realized_pnl_usdt for entry in entries]
        winning = sum(value > 0 for value in pnl_values)
        losing = sum(value < 0 for value in pnl_values)
        breakeven = sum(value == 0 for value in pnl_values)
        count = len(entries)
        net = sum(pnl_values, Decimal("0"))
        win_rate = (
            (Decimal(winning) / Decimal(count) * Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if count
            else Decimal("0")
        )
        average = net / Decimal(count) if count else None
        return PerformanceReportRow(
            dimension=dimension,
            key=key,
            label=label,
            period_started_on=period_started_on,
            period_ended_on=period_ended_on,
            closed_trade_count=count,
            winning_trades=winning,
            losing_trades=losing,
            breakeven_trades=breakeven,
            win_rate_percent=win_rate,
            gross_realized_pnl_usdt=sum(
                (entry.gross_realized_pnl_usdt for entry in entries), Decimal("0")
            ),
            realized_pnl_usdt=net,
            commission_usdt=sum((entry.commission_usdt for entry in entries), Decimal("0")),
            funding_fees_usdt=sum((entry.funding_fees_usdt for entry in entries), Decimal("0")),
            average_trade_pnl_usdt=average,
            best_trade_pnl_usdt=max(pnl_values, default=None),
            worst_trade_pnl_usdt=min(pnl_values, default=None),
        )
