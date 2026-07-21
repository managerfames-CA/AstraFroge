"""Focused BE-08 exchange-source verification tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.schemas.execution import (
    DemoProtectionState,
    DemoTradeCloseReason,
    DemoTradeLifecycle,
    DemoTradeRecord,
)
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.services.journal_exchange_verification import (
    JournalExchangeEvidence,
    JournalExchangeVerificationService,
    JournalSourceVerificationError,
)

NOW = datetime(2026, 7, 19, 15, 0, tzinfo=UTC)


def _trade(
    *,
    lifecycle: DemoTradeLifecycle = DemoTradeLifecycle.CLOSED,
    close_reason: DemoTradeCloseReason | None = DemoTradeCloseReason.TAKE_PROFIT,
    closed_at: datetime | None = NOW,
) -> DemoTradeRecord:
    return DemoTradeRecord(
        trade_id="trade-journal-1",
        signal_id="signal-journal-1",
        symbol="BTCUSDT",
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        lifecycle=lifecycle,
        protection_state=DemoProtectionState.PROTECTED,
        grade=ScannerGrade.A_PLUS,
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("95"),
        take_profit_price=Decimal("110"),
        exit_price=Decimal("110") if closed_at is not None else None,
        exchange_order_id="entry-order-1",
        client_order_id="entry-client-1",
        stop_order_id="stop-algo-1",
        stop_client_order_id="stop-client-1",
        take_profit_order_id="tp-algo-1",
        take_profit_client_order_id="tp-client-1",
        requested_quantity=Decimal("1"),
        executed_quantity=Decimal("1"),
        order_status="FILLED",
        tracked_margin_usdt=Decimal("100"),
        realized_pnl_usdt=Decimal("10"),
        gross_realized_pnl_usdt=Decimal("10.2"),
        commission_usdt=Decimal("-0.2"),
        opened_at=NOW - timedelta(hours=1),
        closed_at=closed_at,
        closed_reason=close_reason,
        updated_at=NOW,
    )


def _entry() -> dict[str, Any]:
    return {
        "clientOrderId": "entry-client-1",
        "orderId": "entry-order-1",
        "status": "FILLED",
        "executedQty": "1",
    }


def _close() -> dict[str, Any]:
    return {
        "clientOrderId": "tp-client-1",
        "actualOrderId": "close-order-1",
        "status": "FINISHED",
    }


def _fills() -> list[dict[str, Any]]:
    return [
        {
            "symbol": "BTCUSDT",
            "id": "entry-fill-1",
            "orderId": "entry-order-1",
            "qty": "1",
            "price": "100",
        },
        {
            "symbol": "BTCUSDT",
            "id": "close-fill-1",
            "orderId": "close-order-1",
            "qty": "1",
            "price": "110",
        },
    ]


def _income() -> list[dict[str, Any]]:
    return [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "10",
            "tranId": "income-pnl-1",
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "COMMISSION",
            "income": "-0.2",
            "tranId": "income-commission-1",
        },
    ]


class _Client:
    def __init__(
        self,
        *,
        entry: dict[str, Any] | None = None,
        close: dict[str, Any] | None = None,
        fills: list[dict[str, Any]] | None = None,
        income: list[dict[str, Any]] | None = None,
    ) -> None:
        self.entry = _entry() if entry is None else entry
        self.close = _close() if close is None else close
        self.fills = _fills() if fills is None else fills
        self.income = _income() if income is None else income
        self.regular_queries: list[str] = []
        self.algo_queries: list[str] = []

    def query_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]:
        self.regular_queries.append(orig_client_order_id)
        if orig_client_order_id == "entry-client-1":
            return dict(self.entry)
        return dict(self.close)

    def query_algo_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]:
        self.algo_queries.append(orig_client_order_id)
        return dict(self.close)

    def user_trades(
        self,
        *,
        symbol: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return list(self.fills)

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return list(self.income)


def _verify(
    client: _Client,
    trade: DemoTradeRecord | None = None,
) -> JournalExchangeEvidence:
    return JournalExchangeVerificationService(
        client,
        now_provider=lambda: NOW,
    ).verify(trade or _trade())


def test_verified_take_profit_record_returns_exchange_evidence() -> None:
    client = _Client()

    evidence = _verify(client)

    assert evidence.checked_at == NOW
    assert evidence.entry_exchange_order_id == "entry-order-1"
    assert evidence.close_exchange_order_id == "close-order-1"
    assert evidence.entry_fill_ids == ("entry-fill-1",)
    assert evidence.close_fill_ids == ("close-fill-1",)
    assert evidence.income_transaction_ids == (
        "income-commission-1",
        "income-pnl-1",
    )
    assert client.regular_queries == ["entry-client-1"]
    assert client.algo_queries == ["tp-client-1"]


def test_manual_close_uses_regular_close_order_identity() -> None:
    client = _Client(
        close={
            "clientOrderId": "af-m-signal-journal-1",
            "orderId": "close-order-1",
            "status": "FILLED",
            "executedQty": "1",
        }
    )

    evidence = _verify(
        client,
        _trade(close_reason=DemoTradeCloseReason.MANUAL_CLOSE),
    )

    assert evidence.close_exchange_order_id == "close-order-1"
    assert client.regular_queries == [
        "entry-client-1",
        "af-m-signal-journal-1",
    ]
    assert client.algo_queries == []


@pytest.mark.parametrize(
    "trade",
    [
        _trade(lifecycle=DemoTradeLifecycle.OPEN, closed_at=None),
        _trade(closed_at=None),
    ],
)
def test_non_closed_candidates_are_rejected(trade: DemoTradeRecord) -> None:
    with pytest.raises(
        JournalSourceVerificationError,
        match="JOURNAL_TRADE_NOT_CLOSED",
    ):
        JournalExchangeVerificationService(_Client()).verify(trade)


def test_missing_private_client_is_rejected() -> None:
    with pytest.raises(
        JournalSourceVerificationError,
        match="DEMO_PRIVATE_API_NOT_CONFIGURED",
    ):
        JournalExchangeVerificationService(None).verify(_trade())


def test_missing_close_reason_is_rejected() -> None:
    with pytest.raises(
        JournalSourceVerificationError,
        match="JOURNAL_CLOSE_REASON_UNVERIFIED",
    ):
        _verify(_Client(), _trade(close_reason=None))


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "clientOrderId": "wrong",
                "orderId": "entry-order-1",
                "status": "FILLED",
                "executedQty": "1",
            },
            "JOURNAL_ENTRY_ORDER_IDENTITY_INVALID",
        ),
        (
            {
                "clientOrderId": "entry-client-1",
                "orderId": "entry-order-1",
                "status": "NEW",
                "executedQty": "0",
            },
            "JOURNAL_ENTRY_ORDER_NOT_FILLED",
        ),
        (
            {
                "clientOrderId": "entry-client-1",
                "orderId": "entry-order-1",
                "status": "FILLED",
                "executedQty": "0.5",
            },
            "JOURNAL_ENTRY_ORDER_QUANTITY_MISMATCH",
        ),
    ],
)
def test_entry_order_must_match_verified_exchange_truth(
    payload: dict[str, Any],
    expected: str,
) -> None:
    with pytest.raises(JournalSourceVerificationError, match=expected):
        _verify(_Client(entry=payload))


@pytest.mark.parametrize(
    ("fills", "expected"),
    [
        ([], "JOURNAL_ENTRY_FILL_MISSING"),
        (
            [
                {
                    "symbol": "BTCUSDT",
                    "id": "same",
                    "orderId": "entry-order-1",
                    "qty": "0.5",
                    "price": "100",
                },
                {
                    "symbol": "BTCUSDT",
                    "id": "same",
                    "orderId": "entry-order-1",
                    "qty": "0.5",
                    "price": "100",
                },
            ],
            "JOURNAL_ENTRY_FILL_IDENTITY_INVALID",
        ),
        (
            [
                {
                    "symbol": "BTCUSDT",
                    "id": "entry-fill-1",
                    "orderId": "entry-order-1",
                    "qty": "0.5",
                    "price": "100",
                }
            ],
            "JOURNAL_ENTRY_FILL_QUANTITY_MISMATCH",
        ),
        (
            [
                {
                    "symbol": "BTCUSDT",
                    "id": "entry-fill-1",
                    "orderId": "entry-order-1",
                    "qty": "NaN",
                    "price": "100",
                }
            ],
            "JOURNAL_EXCHANGE_DECIMAL_INVALID",
        ),
    ],
)
def test_entry_fill_set_must_be_complete_and_valid(
    fills: list[dict[str, Any]],
    expected: str,
) -> None:
    close_fill = {
        "symbol": "BTCUSDT",
        "id": "close-fill-1",
        "orderId": "close-order-1",
        "qty": "1",
        "price": "110",
    }
    with pytest.raises(JournalSourceVerificationError, match=expected):
        _verify(_Client(fills=[*fills, close_fill]))


@pytest.mark.parametrize(
    ("income", "expected"),
    [
        ([], "JOURNAL_REALIZED_PNL_INCOME_MISSING"),
        (
            [
                {
                    "symbol": "BTCUSDT",
                    "incomeType": "REALIZED_PNL",
                    "income": "10",
                    "tranId": "same",
                },
                {
                    "symbol": "BTCUSDT",
                    "incomeType": "COMMISSION",
                    "income": "-0.2",
                    "tranId": "same",
                },
            ],
            "JOURNAL_INCOME_IDENTITY_INVALID",
        ),
        (
            [
                {
                    "symbol": "BTCUSDT",
                    "incomeType": "REALIZED_PNL",
                    "income": "NaN",
                    "tranId": "pnl-1",
                }
            ],
            "JOURNAL_EXCHANGE_DECIMAL_INVALID",
        ),
    ],
)
def test_income_records_require_unique_realized_pnl_evidence(
    income: list[dict[str, Any]],
    expected: str,
) -> None:
    with pytest.raises(JournalSourceVerificationError, match=expected):
        _verify(_Client(income=income))


def test_fill_window_at_exchange_limit_is_rejected() -> None:
    client = _Client(
        fills=[
            {
                "symbol": "OTHER",
                "id": str(index),
                "orderId": str(index),
                "qty": "1",
                "price": "1",
            }
            for index in range(1000)
        ]
    )

    with pytest.raises(
        JournalSourceVerificationError,
        match="JOURNAL_FILL_WINDOW_TRUNCATED",
    ):
        _verify(client)


def test_income_window_at_exchange_limit_is_rejected() -> None:
    client = _Client(
        income=[
            {
                "symbol": "OTHER",
                "incomeType": "COMMISSION",
                "income": "0",
                "tranId": str(index),
            }
            for index in range(1000)
        ]
    )

    with pytest.raises(
        JournalSourceVerificationError,
        match="JOURNAL_INCOME_WINDOW_TRUNCATED",
    ):
        _verify(client)
