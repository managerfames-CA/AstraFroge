"""Scanner-to-Signal stop propagation and daily PnL baseline regressions."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.schemas.risk import RiskDecision
from app.schemas.scanner import (
    CandidateLifecycle,
    ScannerDirection,
    ScannerSetup,
)
from app.services.scanner import ScannerService
from app.services.scanner_scoring import ScannerEngine
from app.services.signals import SignalService
from tests.unit.scanner_test_support import (
    FakeClock,
    FakeIndicators,
    FakeMarket,
    FakeUniverse,
    _candidate_for_service,
    _prepare_setup,
)
from tests.unit.test_real_risk_engine import StubPrivateClient, _service, _signal


@pytest.mark.parametrize(
    ("setup", "direction"),
    [
        (setup, direction)
        for setup in ScannerSetup
        for direction in (ScannerDirection.LONG, ScannerDirection.SHORT)
    ],
)
def test_approved_setup_invalidation_boundary_reaches_signal(
    setup: ScannerSetup,
    direction: ScannerDirection,
) -> None:
    context = _prepare_setup(direction, setup)
    matches = ScannerEngine().setups(context)
    match = next(item for item in matches if item.setup is setup)
    candidate = _candidate_for_service(
        lifecycle=CandidateLifecycle.QUALIFIED,
    ).model_copy(
        update={
            "candidate_id": f"{setup.value}-{direction.value}".ljust(64, "x")[:64],
            "direction": direction,
            "setup": setup,
            "setup_name": setup.value.replace("_", " ").title(),
            "reference_close_time": match.reference_close_time,
            "setup_confirmed_at": match.setup_confirmed_at,
            "expires_at": match.expires_at,
            "level": match.level,
            "selected_ema": match.selected_ema,
            "entry_trigger_price": match.entry_trigger_price,
            "evidence": dict(match.evidence),
        }
    )
    scanner = ScannerService(
        FakeMarket(),
        FakeUniverse(),
        FakeIndicators(),
        clock=FakeClock(),
    )
    scanner._candidates[candidate.candidate_id] = candidate
    scanner._candidate_contexts[candidate.candidate_id] = context

    stop = scanner.risk_stop_price(candidate.candidate_id)
    signal = SignalService(scanner).signals().signals[0]

    assert stop is not None
    assert signal.stop_loss_price == stop
    if direction is ScannerDirection.LONG:
        assert stop < signal.entry_trigger_price
    else:
        assert stop > signal.entry_trigger_price


def test_risk_stop_price_fails_closed_without_complete_context() -> None:
    scanner = ScannerService(
        FakeMarket(),
        FakeUniverse(),
        FakeIndicators(),
        clock=FakeClock(),
    )
    candidate = _candidate_for_service(lifecycle=CandidateLifecycle.QUALIFIED)

    assert scanner.risk_stop_price(candidate.candidate_id) is None

    scanner._candidates[candidate.candidate_id] = candidate
    assert scanner.risk_stop_price(candidate.candidate_id) is None

    context = _prepare_setup(ScannerDirection.LONG, ScannerSetup.TREND_PULLBACK)
    scanner._candidate_contexts[candidate.candidate_id] = context
    scanner._candidates[candidate.candidate_id] = candidate.model_copy(
        update={"evidence": {"pullback_swing_low": "not-a-number"}}
    )
    assert scanner.risk_stop_price(candidate.candidate_id) is None


def test_carried_position_unrealized_pnl_is_excluded_from_daily_lock() -> None:
    client = StubPrivateClient(
        account={
            "canTrade": True,
            "totalWalletBalance": "1000",
            "availableBalance": "800",
            "totalUnrealizedProfit": "-100",
            "totalInitialMargin": "50",
        },
        positions=[
            {"symbol": "XRPUSDT", "positionAmt": "5", "leverage": "10"},
            {"symbol": "BTCUSDT", "positionAmt": "0", "leverage": "10"},
        ],
        income=[],
    )

    service = _service([_signal()], client)
    assessment = service.assessments().assessments[0]
    status = service.status()

    assert assessment.decision is RiskDecision.APPROVED
    assert assessment.daily_realized_pnl_usdt == Decimal("0")
    assert assessment.daily_unrealized_pnl_usdt == Decimal("0")
    assert assessment.daily_net_pnl_usdt == Decimal("0")
    assert status.daily_unrealized_pnl_usdt == Decimal("0")
    assert status.daily_net_pnl_usdt == Decimal("0")
    assert status.lock_reason is None
