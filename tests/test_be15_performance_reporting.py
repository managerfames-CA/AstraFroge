"""Focused BE-15 verified performance reporting tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from app.schemas.journal_performance import (
    JournalEntry,
    JournalEntryList,
    JournalPerformanceState,
)
from app.schemas.performance_reporting import PerformanceDimension
from app.services.performance_reporting import VerifiedPerformanceReportingService


class _JournalStub:
    def __init__(self, entries: list[JournalEntry]) -> None:
        self._entries = entries

    def journal(self, _filters: object) -> JournalEntryList:
        return JournalEntryList(
            count=len(self._entries),
            entries=self._entries,
            source_state=JournalPerformanceState.READY,
            candidate_count=len(self._entries) + 1,
            rejected_count=1,
            rejection_codes=["UNVERIFIED_CANDIDATE"],
        )


def _entry(
    trade_id: str,
    *,
    symbol: str,
    strategy: str,
    strategy_name: str,
    closed_at: datetime,
    pnl: str,
    gross: str,
    commission: str,
    funding: str,
    verified: bool = True,
) -> JournalEntry:
    return JournalEntry.model_construct(
        trade_id=trade_id,
        signal_id=f"signal-{trade_id}",
        symbol=symbol,
        direction=cast(Any, SimpleNamespace(value="LONG")),
        setup=cast(Any, SimpleNamespace(value=strategy)),
        setup_name=strategy_name,
        grade=None,
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        verified_fill_quantity=Decimal("1"),
        realized_pnl_usdt=Decimal(pnl),
        gross_realized_pnl_usdt=Decimal(gross),
        tracked_margin_usdt=Decimal("10"),
        commission_usdt=Decimal(commission),
        funding_fees_usdt=Decimal(funding),
        actual_costs_verified=verified,
        opened_at=closed_at,
        closed_at=closed_at,
        closed_reason=cast(Any, SimpleNamespace(value="TAKE_PROFIT")),
        hold_minutes=0,
        source_verified=verified,
    )


def test_reports_strategy_symbol_daily_weekly_and_monthly() -> None:
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    entries = [
        _entry(
            "one",
            symbol="BTCUSDT",
            strategy="PULLBACK",
            strategy_name="Pullback",
            closed_at=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
            pnl="8",
            gross="10",
            commission="-1",
            funding="-1",
        ),
        _entry(
            "two",
            symbol="BTCUSDT",
            strategy="PULLBACK",
            strategy_name="Pullback",
            closed_at=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
            pnl="-5",
            gross="-4",
            commission="-1",
            funding="0",
        ),
        _entry(
            "three",
            symbol="ETHUSDT",
            strategy="BREAKOUT",
            strategy_name="Breakout",
            closed_at=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
            pnl="3",
            gross="4",
            commission="-1",
            funding="0",
        ),
    ]

    report = VerifiedPerformanceReportingService(
        _JournalStub(entries),  # type: ignore[arg-type]
        now_provider=lambda: now,
    ).report(lookback_days=30)

    assert report.verified_count == 3
    assert report.strategy.dimension is PerformanceDimension.STRATEGY
    assert report.strategy.count == 2
    pullback = next(row for row in report.strategy.rows if row.key == "PULLBACK")
    assert pullback.closed_trade_count == 2
    assert pullback.realized_pnl_usdt == Decimal("3")
    assert pullback.win_rate_percent == Decimal("50.00")
    assert report.symbol.count == 2
    assert report.daily.count == 3
    assert report.weekly.count == 3
    assert report.monthly.count == 1


def test_unverified_or_unverified_cost_entries_are_excluded() -> None:
    now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    rejected = _entry(
        "unsafe",
        symbol="BTCUSDT",
        strategy="PULLBACK",
        strategy_name="Pullback",
        closed_at=now,
        pnl="999",
        gross="999",
        commission="0",
        funding="0",
        verified=False,
    )

    report = VerifiedPerformanceReportingService(
        _JournalStub([rejected]),  # type: ignore[arg-type]
        now_provider=lambda: now,
    ).report(lookback_days=30)

    assert report.verified_count == 0
    assert report.strategy.rows == []
    assert report.symbol.rows == []
    assert report.daily.rows == []
