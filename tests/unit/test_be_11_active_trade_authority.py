"""Focused BE-11 exchange-authoritative Active Trades tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.schemas.execution import (
    DemoProtectionState,
    DemoTradeLifecycle,
    DemoTradeRecord,
)
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.trade_management import (
    ManagedTradeRecordList,
    TradeListFilters,
    TradeManagementState,
    TradeManagementStatusResponse,
    TradeManagementSummary,
)
from app.services.account_snapshot import (
    AccountPositionSnapshot,
    AccountSnapshot,
    AccountSnapshotPayloadError,
)
from app.services.active_trade_authority import ActiveTradeAuthorityService

NOW = datetime(2026, 7, 19, 17, 30, tzinfo=UTC)


def _trade(
    *,
    trade_id: str = "a" * 36,
    symbol: str = "BTCUSDT",
    direction: ScannerDirection = ScannerDirection.LONG,
    quantity: Decimal = Decimal("0.25"),
) -> DemoTradeRecord:
    return DemoTradeRecord(
        trade_id=trade_id,
        signal_id=trade_id,
        symbol=symbol,
        direction=direction,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        lifecycle=DemoTradeLifecycle.OPEN,
        protection_state=DemoProtectionState.PROTECTED,
        grade=ScannerGrade.A_PLUS,
        entry_price=Decimal("999"),
        stop_loss_price=Decimal("95"),
        take_profit_price=Decimal("110"),
        exchange_order_id="entry-order",
        client_order_id="entry-client",
        stop_order_id="stop-order",
        stop_client_order_id="stop-client",
        take_profit_order_id="take-order",
        take_profit_client_order_id="take-client",
        requested_quantity=quantity,
        executed_quantity=quantity,
        order_status="FILLED",
        tracked_margin_usdt=Decimal("25"),
        unrealized_pnl_usdt=Decimal("999"),
        opened_at=NOW,
        updated_at=NOW,
    )


class _History:
    def __init__(self, trades: list[DemoTradeRecord]) -> None:
        self._trades = trades

    def status(self) -> TradeManagementStatusResponse:
        return TradeManagementStatusResponse(
            state=TradeManagementState.READY,
            execution_engine_state="READY",
            max_open_trades_limit=4,
            tracked_trade_count=len(self._trades),
            open_trade_count=len(self._trades),
            available_tracking_slots=max(0, 4 - len(self._trades)),
            updated_at=NOW,
            summary=TradeManagementSummary(),
        )

    def trades(self, filters: TradeListFilters) -> ManagedTradeRecordList:
        return ManagedTradeRecordList(count=len(self._trades), trades=self._trades)


class _Snapshots:
    def __init__(
        self,
        positions: tuple[AccountPositionSnapshot, ...],
        *,
        fail: bool = False,
    ) -> None:
        self.positions = positions
        self.fail = fail

    def force_refresh(self) -> AccountSnapshot:
        if self.fail:
            raise AccountSnapshotPayloadError("bad snapshot")
        return AccountSnapshot(
            snapshot_id="snapshot-1",
            captured_at=NOW,
            source="BINANCE_DEMO_PRIVATE_API",
            source_healthy=True,
            can_trade=True,
            total_wallet_balance_usdt=Decimal("1000"),
            available_balance_usdt=Decimal("900"),
            total_unrealized_pnl_usdt=Decimal("3.5"),
            total_initial_margin_usdt=Decimal("25"),
            balances=(),
            positions=self.positions,
            income=(),
        )


def _position(
    *,
    symbol: str = "BTCUSDT",
    amount: Decimal = Decimal("0.25"),
    entry: Decimal = Decimal("101"),
    unrealized: Decimal = Decimal("3.5"),
) -> AccountPositionSnapshot:
    return AccountPositionSnapshot(
        symbol=symbol,
        position_amount=amount,
        leverage=5,
        entry_price=entry,
        unrealized_pnl=unrealized,
    )


def test_active_trade_uses_current_exchange_position_economics() -> None:
    service = ActiveTradeAuthorityService(
        _History([_trade()]),
        _Snapshots((_position(),)),
    )

    result = service.trades(TradeListFilters())
    trade = result.trades[0]

    assert result.source_state is TradeManagementState.READY
    assert result.exchange_authoritative_open_trades is True
    assert result.position_snapshot_id == "snapshot-1"
    assert trade.exchange_position_verified is True
    assert trade.entry_price == Decimal("101")
    assert trade.executed_quantity == Decimal("0.25")
    assert trade.unrealized_pnl_usdt == Decimal("3.5")
    assert trade.position_source == "BINANCE_DEMO_PRIVATE_API"


def test_status_summary_uses_verified_exchange_values() -> None:
    service = ActiveTradeAuthorityService(
        _History([_trade()]),
        _Snapshots((_position(unrealized=Decimal("7.25")),)),
    )

    status = service.status()

    assert status.state is TradeManagementState.READY
    assert status.exchange_authoritative_active_trades is True
    assert status.open_trade_count == 1
    assert status.summary.long_demo == 1
    assert status.summary.combined_unrealized_pnl_usdt == Decimal("7.25")


def test_quantity_mismatch_fails_closed() -> None:
    service = ActiveTradeAuthorityService(
        _History([_trade()]),
        _Snapshots((_position(amount=Decimal("0.20")),)),
    )

    result = service.trades(TradeListFilters())

    assert result.trades == []
    assert result.source_state is TradeManagementState.SOURCE_VERIFICATION_INCOMPLETE
    assert result.exchange_authoritative_open_trades is False
    assert result.rejection_codes == ["ACTIVE_TRADES_QUANTITY_MISMATCH"]


def test_direction_mismatch_fails_closed() -> None:
    service = ActiveTradeAuthorityService(
        _History([_trade()]),
        _Snapshots((_position(amount=Decimal("-0.25")),)),
    )

    result = service.trades(TradeListFilters())

    assert result.trades == []
    assert "ACTIVE_TRADES_DIRECTION_MISMATCH" in result.rejection_codes


def test_missing_and_orphan_positions_fail_closed() -> None:
    missing = ActiveTradeAuthorityService(
        _History([_trade()]),
        _Snapshots(()),
    ).trades(TradeListFilters())
    orphan = ActiveTradeAuthorityService(
        _History([]),
        _Snapshots((_position(),)),
    ).trades(TradeListFilters())

    assert missing.trades == []
    assert "ACTIVE_TRADES_POSITION_MISSING" in missing.rejection_codes
    assert orphan.trades == []
    assert "ACTIVE_TRADES_ORPHAN_EXCHANGE_POSITION" in orphan.rejection_codes


def test_unavailable_snapshot_never_returns_process_open_trade() -> None:
    service = ActiveTradeAuthorityService(
        _History([_trade()]),
        _Snapshots((), fail=True),
    )

    result = service.trades(TradeListFilters())

    assert result.trades == []
    assert result.source_state is TradeManagementState.EXCHANGE_VERIFICATION_UNAVAILABLE
    assert result.rejected_open_trade_count == 1


def test_duplicate_local_symbol_fails_closed() -> None:
    service = ActiveTradeAuthorityService(
        _History([_trade(), _trade(trade_id="b" * 36)]),
        _Snapshots((_position(),)),
    )

    result = service.trades(TradeListFilters())

    assert result.trades == []
    assert "ACTIVE_TRADES_DUPLICATE_LOCAL_SYMBOL" in result.rejection_codes


def test_filters_apply_after_exchange_verification() -> None:
    service = ActiveTradeAuthorityService(
        _History([_trade()]),
        _Snapshots((_position(),)),
    )

    result = service.trades(
        TradeListFilters(
            symbol="ETHUSDT",
            direction=ScannerDirection.LONG,
            min_grade=ScannerGrade.A,
        )
    )

    assert result.count == 0
    assert result.exchange_authoritative_open_trades is True
