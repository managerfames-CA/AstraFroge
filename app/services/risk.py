"""Fail-closed account-backed Risk Engine derived from stable Signal records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.core.config import Settings
from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.schemas.risk import (
    KillSwitchState,
    RiskAssessment,
    RiskAssessmentList,
    RiskDecision,
    RiskEngineState,
    RiskRejectionCode,
    RiskStatusResponse,
    RiskSummary,
)
from app.schemas.scanner import ScannerDirection, ScannerGrade
from app.schemas.signals import SignalLifecycle, SignalRecord
from app.services.signals import SignalService

_D0 = Decimal("0")
_D100 = Decimal("100")
_TRADING_INCOME_TYPES = frozenset({"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"})


class RiskPrivateClient(Protocol):
    """Private Demo data required for account-authoritative risk decisions."""

    def account(self) -> dict[str, Any]: ...

    def positions(self) -> list[dict[str, Any]]: ...

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class _OpenPosition:
    symbol: str
    direction: ScannerDirection


@dataclass(frozen=True)
class _AccountSnapshot:
    captured_at: datetime
    can_trade: bool
    wallet_balance_usdt: Decimal
    available_balance_usdt: Decimal
    daily_realized_pnl_usdt: Decimal
    daily_unrealized_pnl_usdt: Decimal
    daily_net_pnl_usdt: Decimal
    daily_pnl_percent: Decimal
    current_margin_exposure_usdt: Decimal
    open_positions: tuple[_OpenPosition, ...]
    leverage_by_symbol: dict[str, int]


class _SnapshotError(RuntimeError):
    def __init__(self, code: RiskRejectionCode) -> None:
        super().__init__(code.value)
        self.code = code


class RiskService:
    """Evaluate Signals against verified Demo account and deterministic policy limits."""

    def __init__(
        self,
        signal_service: SignalService,
        settings: Settings,
        private_client: RiskPrivateClient | None = None,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._signals = signal_service
        self._settings = settings
        self._private_client = private_client
        self._now = now_provider or (lambda: datetime.now(UTC))

    def status(self) -> RiskStatusResponse:
        signal_status = self._signals.status()
        snapshot, snapshot_error = self._snapshot_or_error()
        assessments = self._build_assessments(snapshot, snapshot_error)
        summary = RiskSummary(
            approved=sum(item.decision is RiskDecision.APPROVED for item in assessments),
            blocked=sum(item.decision is RiskDecision.BLOCKED for item in assessments),
            watch=sum(item.decision is RiskDecision.WATCH for item in assessments),
            terminal=sum(item.decision is RiskDecision.TERMINAL for item in assessments),
        )
        lock_reason = self._global_lock(snapshot, snapshot_error)
        state = self._engine_state(signal_status.state.value, snapshot, lock_reason)
        open_position_count = len(snapshot.open_positions) if snapshot is not None else 0
        available_slots = max(
            0,
            self._settings.risk_max_open_trades - open_position_count,
        )
        updated_at = snapshot.captured_at if snapshot is not None else signal_status.updated_at
        return RiskStatusResponse(
            state=state,
            contract_version="1",
            signal_engine_state=signal_status.state.value,
            account_snapshot_available=snapshot is not None,
            account_can_trade=snapshot.can_trade if snapshot is not None else False,
            wallet_balance_usdt=(
                snapshot.wallet_balance_usdt if snapshot is not None else None
            ),
            available_balance_usdt=(
                snapshot.available_balance_usdt if snapshot is not None else None
            ),
            daily_realized_pnl_usdt=(
                snapshot.daily_realized_pnl_usdt if snapshot is not None else None
            ),
            daily_unrealized_pnl_usdt=(
                snapshot.daily_unrealized_pnl_usdt if snapshot is not None else None
            ),
            daily_net_pnl_usdt=(
                snapshot.daily_net_pnl_usdt if snapshot is not None else None
            ),
            daily_pnl_percent=(
                snapshot.daily_pnl_percent if snapshot is not None else None
            ),
            risk_per_trade_percent=self._settings.risk_per_trade_percent,
            daily_loss_limit_percent=self._settings.risk_daily_loss_limit_percent,
            daily_profit_lock_percent=self._settings.risk_daily_profit_lock_percent,
            current_margin_exposure_usdt=(
                snapshot.current_margin_exposure_usdt if snapshot is not None else _D0
            ),
            max_margin_exposure_usdt=self._settings.risk_max_margin_exposure_usdt,
            open_position_count=open_position_count,
            max_open_trades_limit=self._settings.risk_max_open_trades,
            available_tracking_slots=available_slots,
            emergency_kill_switch=(
                KillSwitchState.ON if lock_reason is not None else KillSwitchState.OFFLINE
            ),
            lock_reason=lock_reason.value if lock_reason is not None else None,
            updated_at=updated_at,
            summary=summary,
        )

    def assessments(self) -> RiskAssessmentList:
        snapshot, snapshot_error = self._snapshot_or_error()
        assessments = self._build_assessments(snapshot, snapshot_error)
        return RiskAssessmentList(count=len(assessments), assessments=assessments)

    def _build_assessments(
        self,
        snapshot: _AccountSnapshot | None,
        snapshot_error: RiskRejectionCode | None,
    ) -> list[RiskAssessment]:
        records = self._signals.signals().signals
        global_lock = self._global_lock(snapshot, snapshot_error)
        reserved_slots = 0
        reserved_margin = _D0
        reserved_symbols: dict[str, ScannerDirection] = {}
        assessments: list[RiskAssessment] = []

        for signal in records:
            if signal.lifecycle is SignalLifecycle.WATCH:
                assessments.append(
                    self._assessment(
                        signal,
                        decision=RiskDecision.WATCH,
                        snapshot=snapshot,
                    )
                )
                continue
            if signal.lifecycle is not SignalLifecycle.ACTIVE:
                assessments.append(
                    self._assessment(
                        signal,
                        decision=RiskDecision.TERMINAL,
                        snapshot=snapshot,
                    )
                )
                continue

            reason = self._signal_contract_rejection(signal)
            if reason is None:
                reason = global_lock
            sizing: tuple[Decimal, Decimal, Decimal, Decimal] | None = None
            if reason is None and snapshot is not None:
                open_direction = self._open_position_direction(snapshot, signal.symbol)
                reserved_direction = reserved_symbols.get(signal.symbol)
                if open_direction is not None:
                    reason = (
                        RiskRejectionCode.SAME_SYMBOL_POSITION_EXISTS
                        if open_direction is signal.direction
                        else RiskRejectionCode.CONFLICTING_SYMBOL_POSITION_EXISTS
                    )
                elif reserved_direction is not None:
                    reason = (
                        RiskRejectionCode.SAME_SYMBOL_POSITION_EXISTS
                        if reserved_direction is signal.direction
                        else RiskRejectionCode.CONFLICTING_SYMBOL_POSITION_EXISTS
                    )
                elif (
                    len(snapshot.open_positions) + reserved_slots
                    >= self._settings.risk_max_open_trades
                ):
                    reason = RiskRejectionCode.MAX_OPEN_TRADES_REACHED
                else:
                    leverage = snapshot.leverage_by_symbol.get(signal.symbol)
                    if leverage is None or leverage < 1:
                        reason = RiskRejectionCode.SYMBOL_LEVERAGE_UNAVAILABLE
                    else:
                        sizing = self._position_sizing(signal, snapshot, leverage)
                        _, _, _, required_margin = sizing
                        projected_margin = (
                            snapshot.current_margin_exposure_usdt
                            + reserved_margin
                            + required_margin
                        )
                        if (
                            projected_margin
                            > self._settings.risk_max_margin_exposure_usdt
                        ):
                            reason = RiskRejectionCode.MAX_MARGIN_EXPOSURE_REACHED
                        elif (
                            required_margin
                            > snapshot.available_balance_usdt - reserved_margin
                        ):
                            reason = RiskRejectionCode.AVAILABLE_BALANCE_INSUFFICIENT

            if reason is not None:
                assessments.append(
                    self._assessment(
                        signal,
                        decision=RiskDecision.BLOCKED,
                        reason=reason,
                        snapshot=snapshot,
                    )
                )
                continue

            assert snapshot is not None
            assert sizing is not None
            risk_budget, stop_distance, quantity, required_margin = sizing
            notional = quantity * signal.entry_trigger_price
            reserved_slots += 1
            reserved_margin += required_margin
            reserved_symbols[signal.symbol] = signal.direction
            assessments.append(
                self._assessment(
                    signal,
                    decision=RiskDecision.APPROVED,
                    snapshot=snapshot,
                    risk_budget=risk_budget,
                    stop_distance=stop_distance,
                    quantity=quantity,
                    notional=notional,
                    required_margin=required_margin,
                )
            )

        return assessments

    def _assessment(
        self,
        signal: SignalRecord,
        *,
        decision: RiskDecision,
        snapshot: _AccountSnapshot | None,
        reason: RiskRejectionCode | None = None,
        risk_budget: Decimal | None = None,
        stop_distance: Decimal | None = None,
        quantity: Decimal | None = None,
        notional: Decimal | None = None,
        required_margin: Decimal | None = None,
    ) -> RiskAssessment:
        audit_codes = list(signal.audit_codes)
        if reason is not None and reason.value not in audit_codes:
            audit_codes.append(reason.value)
        if decision is RiskDecision.APPROVED and "RISK_APPROVED" not in audit_codes:
            audit_codes.append("RISK_APPROVED")
        return RiskAssessment(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            direction=signal.direction,
            setup=signal.setup,
            setup_name=signal.setup_name,
            signal_lifecycle=signal.lifecycle,
            grade=signal.grade,
            score=signal.score,
            confidence=signal.confidence,
            decision=decision,
            blocked_reason=reason.value if reason is not None else None,
            approved_for_execution=decision is RiskDecision.APPROVED,
            entry_trigger_price=signal.entry_trigger_price,
            stop_loss_price=signal.stop_loss_price,
            stop_distance=stop_distance,
            risk_percent=self._settings.risk_per_trade_percent,
            risk_budget_usdt=risk_budget,
            recommended_quantity=quantity,
            position_notional_usdt=notional,
            required_margin_usdt=required_margin,
            wallet_balance_usdt=(
                snapshot.wallet_balance_usdt if snapshot is not None else None
            ),
            available_balance_usdt=(
                snapshot.available_balance_usdt if snapshot is not None else None
            ),
            daily_realized_pnl_usdt=(
                snapshot.daily_realized_pnl_usdt if snapshot is not None else None
            ),
            daily_unrealized_pnl_usdt=(
                snapshot.daily_unrealized_pnl_usdt if snapshot is not None else None
            ),
            daily_net_pnl_usdt=(
                snapshot.daily_net_pnl_usdt if snapshot is not None else None
            ),
            daily_pnl_percent=(
                snapshot.daily_pnl_percent if snapshot is not None else None
            ),
            open_position_count=(
                len(snapshot.open_positions) if snapshot is not None else 0
            ),
            current_margin_exposure_usdt=(
                snapshot.current_margin_exposure_usdt if snapshot is not None else _D0
            ),
            max_open_trades_limit=self._settings.risk_max_open_trades,
            updated_at=self._now(),
            audit_codes=audit_codes,
        )

    def _position_sizing(
        self,
        signal: SignalRecord,
        snapshot: _AccountSnapshot,
        leverage: int,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        assert signal.stop_loss_price is not None
        stop_distance = abs(signal.entry_trigger_price - signal.stop_loss_price)
        risk_budget = (
            snapshot.wallet_balance_usdt
            * self._settings.risk_per_trade_percent
            / _D100
        )
        quantity = risk_budget / stop_distance
        notional = quantity * signal.entry_trigger_price
        required_margin = notional / Decimal(leverage)
        return risk_budget, stop_distance, quantity, required_margin

    @staticmethod
    def _signal_contract_rejection(
        signal: SignalRecord,
    ) -> RiskRejectionCode | None:
        if signal.grade not in {ScannerGrade.A_PLUS, ScannerGrade.A}:
            return RiskRejectionCode.GRADE_NOT_EXECUTABLE
        if signal.stop_loss_price is None:
            return RiskRejectionCode.STOP_LOSS_MISSING
        if signal.direction is ScannerDirection.LONG:
            if signal.stop_loss_price >= signal.entry_trigger_price:
                return RiskRejectionCode.STOP_LOSS_INVALID
        elif signal.stop_loss_price <= signal.entry_trigger_price:
            return RiskRejectionCode.STOP_LOSS_INVALID
        return None

    def _global_lock(
        self,
        snapshot: _AccountSnapshot | None,
        snapshot_error: RiskRejectionCode | None,
    ) -> RiskRejectionCode | None:
        if snapshot_error is not None:
            return snapshot_error
        if snapshot is None:
            return RiskRejectionCode.DEMO_PRIVATE_API_NOT_CONFIGURED
        if not snapshot.can_trade:
            return RiskRejectionCode.ACCOUNT_CANNOT_TRADE
        if snapshot.wallet_balance_usdt <= 0:
            return RiskRejectionCode.ACCOUNT_BALANCE_UNAVAILABLE
        if self._settings.risk_per_trade_percent <= 0:
            return RiskRejectionCode.RISK_PERCENT_NOT_CONFIGURED
        if self._settings.risk_max_margin_exposure_usdt <= 0:
            return RiskRejectionCode.MAX_MARGIN_EXPOSURE_NOT_CONFIGURED
        if (
            self._settings.risk_daily_loss_limit_percent > 0
            and snapshot.daily_pnl_percent
            <= -self._settings.risk_daily_loss_limit_percent
        ):
            return RiskRejectionCode.DAILY_LOSS_LIMIT_REACHED
        if (
            self._settings.risk_daily_profit_lock_percent > 0
            and snapshot.daily_pnl_percent
            >= self._settings.risk_daily_profit_lock_percent
        ):
            return RiskRejectionCode.DAILY_PROFIT_LOCK_REACHED
        return None

    def _snapshot_or_error(
        self,
    ) -> tuple[_AccountSnapshot | None, RiskRejectionCode | None]:
        if self._private_client is None:
            return None, RiskRejectionCode.DEMO_PRIVATE_API_NOT_CONFIGURED
        try:
            return self._load_snapshot(), None
        except BinanceDemoPrivateClientError:
            return None, RiskRejectionCode.DEMO_PRIVATE_API_UNAVAILABLE
        except _SnapshotError as exc:
            return None, exc.code

    def _load_snapshot(self) -> _AccountSnapshot:
        assert self._private_client is not None
        now = self._now().astimezone(UTC)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        account = self._private_client.account()
        positions = self._private_client.positions()
        income = self._private_client.income_history(
            start_time_ms=int(start_of_day.timestamp() * 1000),
            end_time_ms=int(now.timestamp() * 1000),
            limit=1000,
        )
        if len(income) >= 1000:
            raise _SnapshotError(RiskRejectionCode.PRIVATE_ACCOUNT_PAYLOAD_INVALID)

        can_trade = account.get("canTrade")
        if not isinstance(can_trade, bool):
            raise _SnapshotError(RiskRejectionCode.PRIVATE_ACCOUNT_PAYLOAD_INVALID)
        wallet = self._decimal_field(account, "totalWalletBalance", nonnegative=True)
        available = self._decimal_field(account, "availableBalance", nonnegative=True)
        reported_unrealized = self._decimal_field(account, "totalUnrealizedProfit")
        current_margin = self._decimal_field(
            account,
            "totalInitialMargin",
            nonnegative=True,
        )

        open_positions: list[_OpenPosition] = []
        leverage_by_symbol: dict[str, int] = {}
        for item in positions:
            symbol = item.get("symbol")
            if not isinstance(symbol, str) or not symbol:
                raise _SnapshotError(RiskRejectionCode.PRIVATE_ACCOUNT_PAYLOAD_INVALID)
            amount = self._decimal_field(item, "positionAmt")
            leverage = self._positive_integer_field(item, "leverage")
            leverage_by_symbol[symbol] = leverage
            if amount == 0:
                continue
            open_positions.append(
                _OpenPosition(
                    symbol=symbol,
                    direction=(
                        ScannerDirection.LONG
                        if amount > 0
                        else ScannerDirection.SHORT
                    ),
                )
            )

        realized = _D0
        for item in income:
            income_type = item.get("incomeType")
            if not isinstance(income_type, str):
                raise _SnapshotError(RiskRejectionCode.PRIVATE_ACCOUNT_PAYLOAD_INVALID)
            if income_type not in _TRADING_INCOME_TYPES:
                continue
            realized += self._decimal_field(item, "income")

        # Binance reports cumulative unrealized PnL for open positions, not the
        # change since the current UTC day began. Until a durable start-of-day
        # baseline exists, exclude it from daily policy locks whenever positions
        # are open. This is the fail-closed realized-only policy for carried trades.
        daily_unrealized = reported_unrealized if not open_positions else _D0
        daily_net = realized + daily_unrealized
        day_start_equity = wallet - realized
        daily_percent = (
            daily_net / day_start_equity * _D100
            if day_start_equity > 0
            else _D0
        )
        return _AccountSnapshot(
            captured_at=now,
            can_trade=can_trade,
            wallet_balance_usdt=wallet,
            available_balance_usdt=available,
            daily_realized_pnl_usdt=realized,
            daily_unrealized_pnl_usdt=daily_unrealized,
            daily_net_pnl_usdt=daily_net,
            daily_pnl_percent=daily_percent,
            current_margin_exposure_usdt=current_margin,
            open_positions=tuple(open_positions),
            leverage_by_symbol=leverage_by_symbol,
        )

    @staticmethod
    def _open_position_direction(
        snapshot: _AccountSnapshot,
        symbol: str,
    ) -> ScannerDirection | None:
        for position in snapshot.open_positions:
            if position.symbol == symbol:
                return position.direction
        return None

    @staticmethod
    def _decimal_field(
        payload: dict[str, Any],
        key: str,
        *,
        nonnegative: bool = False,
    ) -> Decimal:
        if key not in payload:
            raise _SnapshotError(RiskRejectionCode.PRIVATE_ACCOUNT_PAYLOAD_INVALID)
        try:
            value = Decimal(str(payload[key]))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise _SnapshotError(
                RiskRejectionCode.PRIVATE_ACCOUNT_PAYLOAD_INVALID
            ) from exc
        if not value.is_finite() or (nonnegative and value < 0):
            raise _SnapshotError(RiskRejectionCode.PRIVATE_ACCOUNT_PAYLOAD_INVALID)
        return value

    @staticmethod
    def _positive_integer_field(payload: dict[str, Any], key: str) -> int:
        value = RiskService._decimal_field(payload, key)
        if value < 1 or value != value.to_integral_value():
            raise _SnapshotError(RiskRejectionCode.PRIVATE_ACCOUNT_PAYLOAD_INVALID)
        return int(value)

    @staticmethod
    def _engine_state(
        signal_state: str,
        snapshot: _AccountSnapshot | None,
        lock_reason: RiskRejectionCode | None,
    ) -> RiskEngineState:
        if signal_state != "READY":
            return RiskEngineState.WAITING_FOR_SIGNALS
        if snapshot is None:
            return RiskEngineState.ACCOUNT_UNAVAILABLE
        if lock_reason is not None:
            return RiskEngineState.POLICY_LOCKED
        return RiskEngineState.READY
