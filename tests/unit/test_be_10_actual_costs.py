"""Focused BE-10 actual commission and funding tests."""

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
from app.services.journal_cost_verification import (
    JournalActualCostEvidence,
    JournalCostVerificationService,
)
from app.services.journal_exchange_verification import (
    JournalExchangeEvidence,
    JournalSourceVerificationError,
)
from app.services.journal_performance import JournalPerformanceService, _VerifiedTrade

NOW = datetime(2026, 7, 19, 17, 0, tzinfo=UTC)


def _trade() -> DemoTradeRecord:
    return DemoTradeRecord(
        trade_id="be10-trade",
        signal_id="be10-signal",
        symbol="BTCUSDT",
        direction=ScannerDirection.LONG,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        lifecycle=DemoTradeLifecycle.CLOSED,
        protection_state=DemoProtectionState.PROTECTED,
        grade=ScannerGrade.A_PLUS,
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("95"),
        take_profit_price=Decimal("110"),
        exit_price=Decimal("110"),
        exchange_order_id="entry-order",
        client_order_id="entry-client",
        stop_order_id="stop-order",
        stop_client_order_id="stop-client",
        take_profit_order_id="take-order",
        take_profit_client_order_id="take-client",
        requested_quantity=Decimal("1"),
        executed_quantity=Decimal("1"),
        order_status="FILLED",
        tracked_margin_usdt=Decimal("100"),
        opened_at=NOW - timedelta(hours=1),
        closed_at=NOW,
        closed_reason=DemoTradeCloseReason.TAKE_PROFIT,
        updated_at=NOW,
    )


def _source() -> JournalExchangeEvidence:
    return JournalExchangeEvidence(
        checked_at=NOW,
        entry_exchange_order_id="entry-order",
        close_exchange_order_id="close-order",
        entry_fill_ids=("entry-fill",),
        close_fill_ids=("close-fill",),
        income_transaction_ids=("source-income",),
        entry_fill_quantity=Decimal("1"),
        close_fill_quantity=Decimal("1"),
        entry_average_price=Decimal("100"),
        close_average_price=Decimal("110"),
        gross_realized_pnl_usdt=Decimal("10"),
    )


def _income(
    *,
    income_type: str,
    amount: str,
    transaction_id: str,
    trade_id: str | None = None,
    asset: str = "USDT",
    time_ms: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "incomeType": income_type,
        "income": amount,
        "tranId": transaction_id,
        "asset": asset,
    }
    if trade_id is not None:
        payload["tradeId"] = trade_id
    if time_ms is not None:
        payload["time"] = time_ms
    return payload


class _Client:
    def __init__(self, income: list[dict[str, Any]]) -> None:
        self.income = income

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return list(self.income)


def _valid_income() -> list[dict[str, Any]]:
    return [
        _income(
            income_type="REALIZED_PNL",
            amount="10",
            transaction_id="pnl",
            trade_id="close-fill",
        ),
        _income(
            income_type="COMMISSION",
            amount="-0.1",
            transaction_id="commission-entry",
            trade_id="entry-fill",
        ),
        _income(
            income_type="COMMISSION",
            amount="-0.2",
            transaction_id="commission-close",
            trade_id="close-fill",
        ),
        _income(
            income_type="FUNDING_FEE",
            amount="-0.5",
            transaction_id="funding",
        ),
    ]


def test_actual_commission_and_funding_produce_net_pnl() -> None:
    evidence = JournalCostVerificationService(
        _Client(_valid_income()),
        now_provider=lambda: NOW,
    ).verify(_trade(), _source())

    assert evidence.realized_pnl_income_usdt == Decimal("10")
    assert evidence.commission_usdt == Decimal("-0.3")
    assert evidence.funding_usdt == Decimal("-0.5")
    assert evidence.net_realized_pnl_usdt == Decimal("9.2")
    assert evidence.commission_transaction_ids == (
        "commission-close",
        "commission-entry",
    )
    assert evidence.funding_transaction_ids == ("funding",)


def test_funding_is_optional_and_defaults_to_zero() -> None:
    income = [item for item in _valid_income() if item["incomeType"] != "FUNDING_FEE"]

    evidence = JournalCostVerificationService(_Client(income)).verify(
        _trade(),
        _source(),
    )

    assert evidence.funding_usdt == Decimal("0")
    assert evidence.net_realized_pnl_usdt == Decimal("9.7")


@pytest.mark.parametrize(
    ("income", "expected"),
    [
        (
            [item for item in _valid_income() if item["incomeType"] != "COMMISSION"],
            "JOURNAL_COMMISSION_INCOME_MISSING",
        ),
        (
            [
                *[item for item in _valid_income() if item["incomeType"] != "REALIZED_PNL"],
                _income(
                    income_type="REALIZED_PNL",
                    amount="9",
                    transaction_id="pnl",
                    trade_id="close-fill",
                ),
            ],
            "JOURNAL_REALIZED_PNL_INCOME_MISMATCH",
        ),
        (
            [
                *[item for item in _valid_income() if item["incomeType"] != "COMMISSION"],
                _income(
                    income_type="COMMISSION",
                    amount="0.1",
                    transaction_id="commission-positive",
                    trade_id="entry-fill",
                ),
            ],
            "JOURNAL_COMMISSION_SIGN_INVALID",
        ),
        (
            [
                *[item for item in _valid_income() if item["incomeType"] != "FUNDING_FEE"],
                _income(
                    income_type="FUNDING_FEE",
                    amount="-0.5",
                    transaction_id="funding",
                    asset="BNB",
                ),
            ],
            "JOURNAL_COST_ASSET_UNSUPPORTED",
        ),
    ],
)
def test_invalid_cost_evidence_fails_closed(
    income: list[dict[str, Any]],
    expected: str,
) -> None:
    with pytest.raises(JournalSourceVerificationError, match=expected):
        JournalCostVerificationService(_Client(income)).verify(
            _trade(),
            _source(),
        )


def test_unrelated_trade_ids_are_not_attributed() -> None:
    income = [
        item
        for item in _valid_income()
        if item["incomeType"] != "COMMISSION"
    ]
    income.append(
        _income(
            income_type="COMMISSION",
            amount="-0.3",
            transaction_id="other-commission",
            trade_id="unrelated-fill",
        )
    )

    with pytest.raises(
        JournalSourceVerificationError,
        match="JOURNAL_COMMISSION_INCOME_MISSING",
    ):
        JournalCostVerificationService(_Client(income)).verify(
            _trade(),
            _source(),
        )


def test_duplicate_income_identity_is_rejected() -> None:
    income = _valid_income()
    income[-1]["tranId"] = "commission-entry"

    with pytest.raises(
        JournalSourceVerificationError,
        match="JOURNAL_INCOME_IDENTITY_INVALID",
    ):
        JournalCostVerificationService(_Client(income)).verify(
            _trade(),
            _source(),
        )


def test_malformed_timestamp_is_rejected() -> None:
    income = _valid_income()
    income[0]["time"] = "not-a-time"

    with pytest.raises(
        JournalSourceVerificationError,
        match="JOURNAL_INCOME_TIMESTAMP_INVALID",
    ):
        JournalCostVerificationService(_Client(income)).verify(
            _trade(),
            _source(),
        )


def test_ambiguous_income_transactions_reject_all_affected_records() -> None:
    shared_costs = JournalActualCostEvidence(
        checked_at=NOW,
        income_transaction_ids=("shared",),
        realized_pnl_transaction_ids=("shared",),
        commission_transaction_ids=(),
        funding_transaction_ids=(),
        realized_pnl_income_usdt=Decimal("10"),
        commission_usdt=Decimal("0"),
        funding_usdt=Decimal("0"),
        net_realized_pnl_usdt=Decimal("10"),
    )
    records = [
        _VerifiedTrade(trade=_trade(), evidence=_source(), costs=shared_costs),
        _VerifiedTrade(trade=_trade(), evidence=_source(), costs=shared_costs),
    ]

    accepted, rejected = JournalPerformanceService._remove_ambiguous_income_records(
        records
    )

    assert accepted == []
    assert rejected == 2
