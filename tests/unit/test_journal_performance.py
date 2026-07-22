"""Journal and Performance Engine unit tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.schemas.execution import (
    DemoProtectionState,
    DemoTradeCloseReason,
    DemoTradeLifecycle,
    DemoTradeRecord,
)
from app.schemas.journal_performance import (
    JournalFilters,
    JournalPerformanceState,
    JournalPnlSource,
)
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.trade_management import (
    ManagedTradeRecordList,
    TradeManagementState,
    TradeManagementStatusResponse,
    TradeManagementSummary,
)
from app.services.journal_performance import JournalPerformanceService

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _trade(
    *,
    trade_id: str,
    signal_id: str,
    symbol: str,
    direction: ScannerDirection,
    lifecycle: DemoTradeLifecycle,
    grade: ScannerGrade,
    entry_price: Decimal,
    exit_price: Decimal | None,
    margin: Decimal,
    unrealized: Decimal,
    realized: Decimal,
    gross_realized: Decimal = Decimal("0"),
    commission: Decimal = Decimal("0"),
    funding: Decimal = Decimal("0"),
    opened_at: datetime,
    updated_at: datetime,
    closed_at: datetime | None = None,
    close_reason: DemoTradeCloseReason | None = None,
) -> DemoTradeRecord:
    stop_loss_price = Decimal("95") if direction is ScannerDirection.LONG else Decimal("105")
    return DemoTradeRecord(
        trade_id=trade_id,
        signal_id=signal_id,
        symbol=symbol,
        direction=direction,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        lifecycle=lifecycle,
        protection_state=DemoProtectionState.PROTECTED,
        grade=grade,
        entry_price=entry_price,
        stop_loss_price=stop_loss_price,
        take_profit_price=(Decimal("110") if direction is ScannerDirection.LONG else Decimal("90")),
        exit_price=exit_price,
        exchange_order_id=f"entry-{trade_id[:4]}",
        client_order_id=f"af-e-{signal_id[:20]}",
        stop_order_id=f"stop-{trade_id[:4]}",
        stop_client_order_id=f"af-s-{signal_id[:20]}",
        take_profit_order_id=f"take-{trade_id[:4]}",
        take_profit_client_order_id=f"af-t-{signal_id[:20]}",
        requested_quantity=Decimal("1"),
        executed_quantity=Decimal("1"),
        order_status="FILLED",
        tracked_margin_usdt=margin,
        unrealized_pnl_usdt=unrealized,
        realized_pnl_usdt=realized,
        gross_realized_pnl_usdt=gross_realized,
        commission_usdt=commission,
        funding_fees_usdt=funding,
        opened_at=opened_at,
        closed_at=closed_at,
        closed_reason=close_reason,
        updated_at=updated_at,
    )


class StubTradeManagement:
    def __init__(self) -> None:
        self._trades = [
            _trade(
                trade_id="a" * 36,
                signal_id="1" * 64,
                symbol="BTCUSDT",
                direction=ScannerDirection.LONG,
                lifecycle=DemoTradeLifecycle.CLOSED,
                grade=ScannerGrade.A_PLUS,
                entry_price=Decimal("101"),
                exit_price=Decimal("105"),
                margin=Decimal("25"),
                unrealized=Decimal("0"),
                realized=Decimal("999"),
                gross_realized=Decimal("998"),
                commission=Decimal("-99"),
                funding=Decimal("-88"),
                opened_at=NOW - timedelta(hours=2),
                closed_at=NOW - timedelta(hours=1),
                close_reason=DemoTradeCloseReason.TAKE_PROFIT,
                updated_at=NOW - timedelta(hours=1),
            ),
            _trade(
                trade_id="b" * 36,
                signal_id="2" * 64,
                symbol="ETHUSDT",
                direction=ScannerDirection.SHORT,
                lifecycle=DemoTradeLifecycle.CLOSED,
                grade=ScannerGrade.A,
                entry_price=Decimal("99"),
                exit_price=Decimal("101"),
                margin=Decimal("15"),
                unrealized=Decimal("0"),
                realized=Decimal("777"),
                gross_realized=Decimal("776"),
                commission=Decimal("-77"),
                funding=Decimal("-66"),
                opened_at=NOW - timedelta(days=2, hours=3),
                closed_at=NOW - timedelta(days=2, hours=1),
                close_reason=DemoTradeCloseReason.STOP_LOSS,
                updated_at=NOW - timedelta(days=2, hours=1),
            ),
            _trade(
                trade_id="c" * 36,
                signal_id="3" * 64,
                symbol="SOLUSDT",
                direction=ScannerDirection.LONG,
                lifecycle=DemoTradeLifecycle.OPEN,
                grade=ScannerGrade.B_PLUS,
                entry_price=Decimal("150"),
                exit_price=None,
                margin=Decimal("10"),
                unrealized=Decimal("1"),
                realized=Decimal("0"),
                opened_at=NOW,
                updated_at=NOW,
            ),
        ]

    def status(self) -> TradeManagementStatusResponse:
        return TradeManagementStatusResponse(
            state=TradeManagementState.READY,
            execution_engine_state="READY",
            max_open_trades_limit=4,
            tracked_trade_count=3,
            open_trade_count=1,
            available_tracking_slots=3,
            updated_at=NOW,
            summary=TradeManagementSummary(manual_demo_trades=1),
        )

    def trades(self, filters: Any) -> ManagedTradeRecordList:
        return ManagedTradeRecordList(
            count=len(self._trades),
            trades=self._trades,
        )


class StubExchangeClient:
    def __init__(self, *, reject_symbol: str | None = None) -> None:
        self._reject_symbol = reject_symbol

    @staticmethod
    def _entry_order_id(symbol: str) -> str:
        return "entry-aaaa" if symbol == "BTCUSDT" else "entry-bbbb"

    @staticmethod
    def _close_order_id(symbol: str) -> str:
        return "take-aaaa" if symbol == "BTCUSDT" else "stop-bbbb"

    def query_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]:
        if symbol == self._reject_symbol:
            return {
                "clientOrderId": orig_client_order_id,
                "orderId": self._entry_order_id(symbol),
                "status": "NEW",
                "executedQty": "0",
            }
        return {
            "clientOrderId": orig_client_order_id,
            "orderId": self._entry_order_id(symbol),
            "status": "FILLED",
            "executedQty": "1",
        }

    def query_algo_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]:
        return {
            "clientOrderId": orig_client_order_id,
            "actualOrderId": self._close_order_id(symbol),
            "status": "FINISHED",
        }

    def user_trades(
        self,
        *,
        symbol: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        close_price = "104" if symbol == "BTCUSDT" else "102"
        return [
            {
                "symbol": symbol,
                "id": f"entry-fill-{symbol}",
                "orderId": self._entry_order_id(symbol),
                "qty": "1",
                "price": "100",
            },
            {
                "symbol": symbol,
                "id": f"close-fill-{symbol}",
                "orderId": self._close_order_id(symbol),
                "qty": "1",
                "price": close_price,
            },
        ]

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        recent_boundary = int((NOW - timedelta(days=1)).timestamp() * 1000)
        symbol = "BTCUSDT" if end_time_ms > recent_boundary else "ETHUSDT"
        realized_pnl = "4" if symbol == "BTCUSDT" else "-2"
        funding = "-0.2" if symbol == "BTCUSDT" else "0.1"
        return [
            {
                "symbol": symbol,
                "incomeType": "REALIZED_PNL",
                "income": realized_pnl,
                "tranId": f"pnl-{symbol}",
                "asset": "USDT",
            },
            {
                "symbol": symbol,
                "incomeType": "COMMISSION",
                "income": "-0.1",
                "tranId": f"commission-{symbol}",
                "asset": "USDT",
            },
            {
                "symbol": symbol,
                "incomeType": "FUNDING_FEE",
                "income": funding,
                "tranId": f"funding-{symbol}",
                "asset": "USDT",
            },
        ]


def _service(*, reject_symbol: str | None = None) -> JournalPerformanceService:
    return JournalPerformanceService(
        StubTradeManagement(),  # type: ignore[arg-type]
        StubExchangeClient(reject_symbol=reject_symbol),
        now_provider=lambda: NOW,
    )


def test_journal_performance_status_uses_actual_cost_net_pnl() -> None:
    status = _service().status()

    assert status.state is JournalPerformanceState.READY
    assert status.verified_fill_pnl_required is True
    assert status.verified_actual_costs_required is True
    assert status.candidate_count == 2
    assert status.verified_count == 2
    assert status.rejected_count == 0
    assert status.summary.closed_trade_count == 2
    assert status.summary.winning_trades == 1
    assert status.summary.losing_trades == 1
    assert status.summary.gross_realized_pnl_usdt == Decimal("2")
    assert status.summary.commission_usdt == Decimal("-0.2")
    assert status.summary.funding_fees_usdt == Decimal("-0.1")
    assert status.summary.realized_pnl_usdt == Decimal("1.7")
    assert status.summary.pnl_source is JournalPnlSource.VERIFIED_FILLS_NET_ACTUAL_COSTS


def test_journal_entries_ignore_process_cost_and_pnl_values() -> None:
    journal = _service().journal(
        JournalFilters(
            symbol="BTCUSDT",
            direction=ScannerDirection.LONG,
            min_grade=ScannerGrade.A,
            close_reason=DemoTradeCloseReason.TAKE_PROFIT,
        )
    )

    assert journal.count == 1
    assert journal.verified_source_only is True
    assert journal.verified_fill_pnl_only is True
    assert journal.verified_actual_costs_only is True
    entry = journal.entries[0]
    assert entry.source_verified is True
    assert entry.actual_costs_verified is True
    assert entry.entry_fill_ids == ["entry-fill-BTCUSDT"]
    assert entry.close_fill_ids == ["close-fill-BTCUSDT"]
    assert entry.entry_price == Decimal("100")
    assert entry.exit_price == Decimal("104")
    assert entry.verified_fill_quantity == Decimal("1")
    assert entry.gross_realized_pnl_usdt == Decimal("4")
    assert entry.commission_usdt == Decimal("-0.1")
    assert entry.funding_fees_usdt == Decimal("-0.2")
    assert entry.realized_pnl_usdt == Decimal("3.7")
    assert entry.pnl_source is JournalPnlSource.VERIFIED_FILLS_NET_ACTUAL_COSTS
    assert entry.commission_transaction_ids == ["commission-BTCUSDT"]
    assert entry.funding_transaction_ids == ["funding-BTCUSDT"]
    assert entry.hold_minutes == 60


def test_journal_performance_window_metrics_use_net_actual_costs() -> None:
    snapshot = _service().performance(lookback_days=30)

    assert snapshot.source_state is JournalPerformanceState.READY
    assert snapshot.verified_fill_pnl_only is True
    assert snapshot.verified_actual_costs_only is True
    assert snapshot.verified_count == 2
    assert snapshot.summary.realized_pnl_usdt == Decimal("1.7")
    assert snapshot.summary.gross_realized_pnl_usdt == Decimal("2")
    assert snapshot.summary.win_rate_percent == Decimal("50.00")
    assert snapshot.summary.average_win_usdt == Decimal("3.7")
    assert snapshot.summary.average_loss_usdt == Decimal("-2.0")
    assert snapshot.summary.best_trade_pnl_usdt == Decimal("3.7")
    assert snapshot.summary.worst_trade_pnl_usdt == Decimal("-2.0")
    assert snapshot.summary.commission_usdt == Decimal("-0.2")
    assert snapshot.summary.funding_fees_usdt == Decimal("-0.1")


def test_unverified_exchange_record_is_rejected_from_journal() -> None:
    service = _service(reject_symbol="ETHUSDT")

    journal = service.journal(JournalFilters())
    status = service.status()

    assert journal.count == 1
    assert journal.entries[0].symbol == "BTCUSDT"
    assert journal.rejected_count == 1
    assert "JOURNAL_ENTRY_ORDER_NOT_FILLED" in journal.rejection_codes
    assert status.state is JournalPerformanceState.SOURCE_VERIFICATION_INCOMPLETE


def test_missing_private_client_returns_no_process_only_records() -> None:
    service = JournalPerformanceService(
        StubTradeManagement(),  # type: ignore[arg-type]
        verification_client=None,
        now_provider=lambda: NOW,
    )

    journal = service.journal(JournalFilters())

    assert journal.count == 0
    assert journal.candidate_count == 2
    assert journal.rejected_count == 2
    assert journal.source_state is JournalPerformanceState.EXCHANGE_VERIFICATION_UNAVAILABLE
    assert journal.rejection_codes == ["DEMO_PRIVATE_API_NOT_CONFIGURED"]
