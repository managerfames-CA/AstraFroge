"""Account-backed Risk Engine sizing, exposure, and lock regression tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import Settings
from app.integrations.binance.private_demo_client import (
    BinanceDemoPrivateClient,
    BinanceDemoPrivateClientError,
)
from app.schemas.risk import RiskDecision, RiskEngineState, RiskRejectionCode
from app.schemas.scanner import (
    CandidateLifecycle,
    ScannerDirection,
    ScannerGrade,
    ScannerSetup,
)
from app.schemas.signals import (
    SignalEngineState,
    SignalLifecycle,
    SignalRecord,
    SignalRecordList,
    SignalStatusResponse,
    SignalSummary,
)
from app.services.risk import RiskService

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


def _settings(**updates: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "risk_per_trade_percent": Decimal("1"),
        "risk_daily_loss_limit_percent": Decimal("3"),
        "risk_daily_profit_lock_percent": Decimal("5"),
        "risk_max_open_trades": 2,
        "risk_max_margin_exposure_usdt": Decimal("500"),
    }
    values.update(updates)
    return Settings(**values)


def _signal(
    key: str = "a",
    *,
    symbol: str = "BTCUSDT",
    direction: ScannerDirection = ScannerDirection.LONG,
    lifecycle: SignalLifecycle = SignalLifecycle.ACTIVE,
    grade: ScannerGrade = ScannerGrade.A_PLUS,
    entry: Decimal = Decimal("100"),
    stop: Decimal | None = Decimal("95"),
) -> SignalRecord:
    return SignalRecord(
        signal_id=key * 64,
        candidate_id=key.upper() * 64,
        symbol=symbol,
        direction=direction,
        setup=ScannerSetup.TREND_PULLBACK,
        setup_name="Trend Pullback",
        lifecycle=lifecycle,
        scanner_lifecycle=(
            CandidateLifecycle.QUALIFIED
            if lifecycle is SignalLifecycle.ACTIVE
            else CandidateLifecycle.WATCH_NEAR
        ),
        grade=grade,
        score=90,
        confidence=80,
        entry_ready=lifecycle is SignalLifecycle.ACTIVE,
        entry_trigger_price=entry,
        stop_loss_price=stop,
        reference_close_time=NOW - timedelta(minutes=15),
        setup_confirmed_at=NOW - timedelta(minutes=15),
        expires_at=NOW + timedelta(minutes=45),
        qualification_expires_at=NOW + timedelta(minutes=15),
        evaluated_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        source_run_id="scanner-run-1",
        universe_rank=1,
        quote_volume=Decimal("100000000"),
        spread_bps=Decimal("1"),
    )


class StubSignals:
    def __init__(self, records: list[SignalRecord]) -> None:
        self.records = records

    def signals(self) -> SignalRecordList:
        return SignalRecordList(count=len(self.records), signals=self.records)

    def status(self) -> SignalStatusResponse:
        active = sum(item.lifecycle is SignalLifecycle.ACTIVE for item in self.records)
        watch = sum(item.lifecycle is SignalLifecycle.WATCH for item in self.records)
        return SignalStatusResponse(
            state=SignalEngineState.READY,
            scanner_state="ON",
            active_signal_count=active,
            watch_signal_count=watch,
            terminal_signal_count=len(self.records) - active - watch,
            updated_at=NOW,
            summary=SignalSummary(active_signals=active),
        )


class StubPrivateClient:
    def __init__(
        self,
        *,
        account: dict[str, Any] | None = None,
        positions: list[dict[str, Any]] | None = None,
        income: list[dict[str, Any]] | None = None,
        fail: bool = False,
    ) -> None:
        self.account_payload = account or {
            "canTrade": True,
            "totalWalletBalance": "1000",
            "availableBalance": "800",
            "totalUnrealizedProfit": "0",
            "totalInitialMargin": "50",
        }
        self.position_payload = positions or [
            {"symbol": "BTCUSDT", "positionAmt": "0", "leverage": "10"},
            {"symbol": "ETHUSDT", "positionAmt": "0", "leverage": "10"},
            {"symbol": "SOLUSDT", "positionAmt": "0", "leverage": "10"},
        ]
        self.income_payload = income or []
        self.fail = fail
        self.income_window: tuple[int, int, int] | None = None

    def account(self) -> dict[str, Any]:
        if self.fail:
            raise BinanceDemoPrivateClientError("private unavailable")
        return self.account_payload

    def positions(self) -> list[dict[str, Any]]:
        if self.fail:
            raise BinanceDemoPrivateClientError("private unavailable")
        return self.position_payload

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if self.fail:
            raise BinanceDemoPrivateClientError("private unavailable")
        self.income_window = (start_time_ms, end_time_ms, limit)
        return self.income_payload


def _service(
    records: list[SignalRecord],
    client: StubPrivateClient | None,
    settings: Settings | None = None,
) -> RiskService:
    return RiskService(
        StubSignals(records),  # type: ignore[arg-type]
        settings or _settings(),
        client,
        now_provider=lambda: NOW,
    )


def test_account_backed_position_sizing_and_status() -> None:
    client = StubPrivateClient(
        income=[
            {"incomeType": "REALIZED_PNL", "income": "5"},
            {"incomeType": "COMMISSION", "income": "-1"},
            {"incomeType": "TRANSFER", "income": "100"},
        ]
    )
    service = _service([_signal()], client)

    assessment = service.assessments().assessments[0]
    status = service.status()

    assert assessment.decision is RiskDecision.APPROVED
    assert assessment.approved_for_execution is True
    assert assessment.risk_budget_usdt == Decimal("10")
    assert assessment.stop_distance == Decimal("5")
    assert assessment.recommended_quantity == Decimal("2")
    assert assessment.position_notional_usdt == Decimal("200")
    assert assessment.required_margin_usdt == Decimal("20")
    assert assessment.current_margin_exposure_usdt == Decimal("50")
    assert assessment.audit_codes[-1] == "RISK_APPROVED"
    assert status.state is RiskEngineState.READY
    assert status.account_snapshot_available is True
    assert status.wallet_balance_usdt == Decimal("1000")
    assert status.available_tracking_slots == 2
    assert status.daily_realized_pnl_usdt == Decimal("4")
    assert status.daily_net_pnl_usdt == Decimal("4")
    assert client.income_window == (
        int(NOW.replace(hour=0).timestamp() * 1000),
        int(NOW.timestamp() * 1000),
        1000,
    )


def test_missing_private_client_and_private_outage_fail_closed() -> None:
    missing = _service([_signal()], None)
    unavailable = _service([_signal()], StubPrivateClient(fail=True))

    missing_assessment = missing.assessments().assessments[0]
    unavailable_assessment = unavailable.assessments().assessments[0]

    assert missing.status().state is RiskEngineState.ACCOUNT_UNAVAILABLE
    assert missing_assessment.blocked_reason == "DEMO_PRIVATE_API_NOT_CONFIGURED"
    assert unavailable_assessment.blocked_reason == "DEMO_PRIVATE_API_UNAVAILABLE"


def test_watch_signal_does_not_consume_actual_open_position_slot() -> None:
    positions = [
        {"symbol": "XRPUSDT", "positionAmt": "5", "leverage": "10"},
        {"symbol": "BTCUSDT", "positionAmt": "0", "leverage": "10"},
        {"symbol": "ETHUSDT", "positionAmt": "0", "leverage": "10"},
    ]
    records = [
        _signal("w", lifecycle=SignalLifecycle.WATCH, grade=ScannerGrade.B_PLUS),
        _signal("a", symbol="BTCUSDT"),
        _signal("b", symbol="ETHUSDT"),
    ]
    service = _service(records, StubPrivateClient(positions=positions))

    assessments = service.assessments().assessments

    assert assessments[0].decision is RiskDecision.WATCH
    assert assessments[1].decision is RiskDecision.APPROVED
    assert assessments[2].blocked_reason == "MAX_OPEN_TRADES_REACHED"
    assert service.status().open_position_count == 1
    assert service.status().available_tracking_slots == 1


def test_existing_and_reserved_symbol_positions_are_blocked() -> None:
    positions = [
        {"symbol": "BTCUSDT", "positionAmt": "1", "leverage": "10"},
        {"symbol": "ETHUSDT", "positionAmt": "-1", "leverage": "10"},
        {"symbol": "SOLUSDT", "positionAmt": "0", "leverage": "10"},
    ]
    records = [
        _signal("a", symbol="BTCUSDT"),
        _signal("b", symbol="ETHUSDT", direction=ScannerDirection.LONG),
        _signal("c", symbol="SOLUSDT"),
        _signal("d", symbol="SOLUSDT"),
    ]
    service = _service(
        records,
        StubPrivateClient(positions=positions),
        _settings(risk_max_open_trades=10),
    )

    assessments = service.assessments().assessments

    assert assessments[0].blocked_reason == "SAME_SYMBOL_POSITION_EXISTS"
    assert assessments[1].blocked_reason == "CONFLICTING_SYMBOL_POSITION_EXISTS"
    assert assessments[2].decision is RiskDecision.APPROVED
    assert assessments[3].blocked_reason == "SAME_SYMBOL_POSITION_EXISTS"


def test_daily_loss_and_profit_locks_use_verified_account_pnl() -> None:
    loss_client = StubPrivateClient(
        account={
            "canTrade": True,
            "totalWalletBalance": "970",
            "availableBalance": "900",
            "totalUnrealizedProfit": "-10",
            "totalInitialMargin": "0",
        },
        income=[{"incomeType": "REALIZED_PNL", "income": "-30"}],
    )
    profit_client = StubPrivateClient(
        account={
            "canTrade": True,
            "totalWalletBalance": "1050",
            "availableBalance": "1000",
            "totalUnrealizedProfit": "10",
            "totalInitialMargin": "0",
        },
        income=[{"incomeType": "REALIZED_PNL", "income": "50"}],
    )

    loss = _service([_signal()], loss_client).assessments().assessments[0]
    profit = _service([_signal()], profit_client).assessments().assessments[0]

    assert loss.blocked_reason == "DAILY_LOSS_LIMIT_REACHED"
    assert loss.daily_net_pnl_usdt == Decimal("-40")
    assert profit.blocked_reason == "DAILY_PROFIT_LOCK_REACHED"
    assert profit.daily_net_pnl_usdt == Decimal("60")


def test_signal_contract_and_configuration_failures_are_explicit() -> None:
    client = StubPrivateClient()
    records = [
        _signal("a", stop=None),
        _signal("b", entry=Decimal("100"), stop=Decimal("105")),
        _signal("c", grade=ScannerGrade.B_PLUS),
    ]
    service = _service(records, client)

    assessments = service.assessments().assessments

    assert assessments[0].blocked_reason == "STOP_LOSS_MISSING"
    assert assessments[1].blocked_reason == "STOP_LOSS_INVALID"
    assert assessments[2].blocked_reason == "GRADE_NOT_EXECUTABLE"

    no_risk = _service(
        [_signal()],
        client,
        _settings(risk_per_trade_percent=Decimal("0")),
    ).assessments().assessments[0]
    no_margin = _service(
        [_signal()],
        client,
        _settings(risk_max_margin_exposure_usdt=Decimal("0")),
    ).assessments().assessments[0]
    assert no_risk.blocked_reason == "RISK_PERCENT_NOT_CONFIGURED"
    assert no_margin.blocked_reason == "MAX_MARGIN_EXPOSURE_NOT_CONFIGURED"


def test_margin_available_balance_and_leverage_gates() -> None:
    low_margin_limit = _service(
        [_signal()],
        StubPrivateClient(),
        _settings(risk_max_margin_exposure_usdt=Decimal("60")),
    ).assessments().assessments[0]
    low_balance = _service(
        [_signal()],
        StubPrivateClient(
            account={
                "canTrade": True,
                "totalWalletBalance": "1000",
                "availableBalance": "5",
                "totalUnrealizedProfit": "0",
                "totalInitialMargin": "0",
            }
        ),
    ).assessments().assessments[0]
    no_leverage = _service(
        [_signal(symbol="BNBUSDT")],
        StubPrivateClient(),
    ).assessments().assessments[0]

    assert low_margin_limit.blocked_reason == "MAX_MARGIN_EXPOSURE_REACHED"
    assert low_balance.blocked_reason == "AVAILABLE_BALANCE_INSUFFICIENT"
    assert no_leverage.blocked_reason == "SYMBOL_LEVERAGE_UNAVAILABLE"


def test_account_cannot_trade_and_malformed_payload_fail_closed() -> None:
    cannot_trade = _service(
        [_signal()],
        StubPrivateClient(
            account={
                "canTrade": False,
                "totalWalletBalance": "1000",
                "availableBalance": "800",
                "totalUnrealizedProfit": "0",
                "totalInitialMargin": "0",
            }
        ),
    )
    malformed = _service(
        [_signal()],
        StubPrivateClient(
            account={
                "canTrade": True,
                "totalWalletBalance": "NaN",
                "availableBalance": "800",
                "totalUnrealizedProfit": "0",
                "totalInitialMargin": "0",
            }
        ),
    )

    assert cannot_trade.status().state is RiskEngineState.POLICY_LOCKED
    assert cannot_trade.status().lock_reason == "ACCOUNT_CANNOT_TRADE"
    assert malformed.assessments().assessments[0].blocked_reason == (
        "PRIVATE_ACCOUNT_PAYLOAD_INVALID"
    )


def test_private_client_income_history_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/income"
        assert request.headers["X-MBX-APIKEY"] == "demo-key"
        assert request.url.params["startTime"] == "100"
        assert request.url.params["endTime"] == "200"
        return httpx.Response(
            200,
            json=[{"incomeType": "REALIZED_PNL", "income": "1"}],
        )

    client = BinanceDemoPrivateClient(
        base_url="https://demo-fapi.binance.com",
        api_key="demo-key",
        api_secret="demo-secret",
        timeout_seconds=1,
        recv_window_ms=5000,
        transport=httpx.MockTransport(handler),
    )

    payload = client.income_history(start_time_ms=100, end_time_ms=200)

    assert payload == [{"incomeType": "REALIZED_PNL", "income": "1"}]
    assert RiskRejectionCode.MAX_OPEN_TRADES_REACHED.value == "MAX_OPEN_TRADES_REACHED"
