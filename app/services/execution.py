"""Truthful, fail-closed Binance Demo Execution Engine orchestration."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.schemas.execution import (
    DemoAccountBalance,
    DemoExecutionAccountResponse,
    DemoExecutionActivateRequest,
    DemoExecutionPlan,
    DemoExecutionPlanList,
    DemoExecutionState,
    DemoExecutionStatusResponse,
    DemoExecutionSummary,
    DemoOrderSide,
    DemoPlanState,
    DemoPositionSnapshot,
    DemoProtectionState,
    DemoTradeLifecycle,
    DemoTradeRecord,
    DemoTradeRecordList,
)
from app.schemas.risk import RiskAssessment, RiskDecision, RiskEngineState
from app.schemas.scanner import ScannerDirection
from app.services.exchange_rules import ExchangeRuleError, parse_symbol_trading_rules
from app.services.risk import RiskService

_ORDER_NOT_FOUND = -2013
_OPEN_PROTECTION_STATUSES = frozenset({"NEW", "PARTIALLY_FILLED"})
_TRADE_STORE_LOGGER = logging.getLogger("astraforge.execution")


class ExecutionPrivateClient(Protocol):
    """Binance Demo operations required by the execution service."""

    def exchange_info(self) -> dict[str, Any]: ...

    def mark_price(self, symbol: str) -> dict[str, Any]: ...

    def position_mode(self) -> dict[str, Any]: ...

    def account(self) -> dict[str, Any]: ...

    def positions(self) -> list[dict[str, Any]]: ...

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]: ...

    def cancel_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]: ...

    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str,
        new_client_order_id: str,
        reduce_only: bool = False,
    ) -> dict[str, Any]: ...

    def place_protective_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str,
        stop_price: str,
        new_client_order_id: str,
    ) -> dict[str, Any]: ...


class DemoExecutionService:
    """Open only verified, protected Binance Demo positions."""

    def __init__(
        self,
        risk_service: RiskService,
        settings: Settings,
        private_client: ExecutionPrivateClient | None = None,
        trade_store_path: Path | None = None,
    ) -> None:
        self._risk = risk_service
        self._settings = settings
        self._private_client = private_client
        self._trade_store_path = trade_store_path
        self._trades: dict[str, DemoTradeRecord] = {}
        self._load_trades()

    def status(self) -> DemoExecutionStatusResponse:
        risk_status = self._risk.status()
        plans = self.plans().plans
        trades = self.trades().trades
        open_trades = [
            trade for trade in trades if trade.lifecycle is DemoTradeLifecycle.OPEN
        ]
        execution_enabled = bool(self._settings.execution_enabled)
        summary = DemoExecutionSummary(
            executable_plans=sum(plan.plan_state is DemoPlanState.EXECUTABLE for plan in plans),
            blocked_plans=sum(plan.plan_state is DemoPlanState.BLOCKED for plan in plans),
            watch_plans=sum(plan.plan_state is DemoPlanState.WATCH for plan in plans),
            open_trades=len(open_trades),
            long_demo=sum(
                trade.direction is ScannerDirection.LONG for trade in open_trades
            ),
            short_demo=sum(
                trade.direction is ScannerDirection.SHORT for trade in open_trades
            ),
        )
        available_slots = max(
            0,
            self._settings.risk_max_open_trades - len(open_trades),
        )
        updated_at = max(
            [plan.updated_at for plan in plans] + [trade.updated_at for trade in trades],
            default=risk_status.updated_at,
        )
        state = self._state_from_risk(risk_status.state)
        execution_integration_available = (
            execution_enabled
            and self._settings.demo_credentials_configured
            and self._private_client is not None
        )
        return DemoExecutionStatusResponse(
            state=state,
            contract_version="1",
            execution_integration_available=execution_integration_available,
            execution_enabled=execution_enabled,
            demo_credentials_configured=self._settings.demo_credentials_configured,
            private_api_available=self._private_client is not None,
            risk_engine_state=risk_status.state.value,
            take_profit_r_multiple=self._settings.execution_take_profit_r_multiple,
            max_open_trades_limit=self._settings.risk_max_open_trades,
            tracked_trade_count=len(trades),
            available_tracking_slots=available_slots,
            combined_unrealized_pnl_usdt=sum(
                (trade.unrealized_pnl_usdt for trade in open_trades),
                Decimal("0"),
            ),
            total_tracked_margin_usdt=sum(
                (trade.tracked_margin_usdt for trade in open_trades),
                Decimal("0"),
            ),
            updated_at=updated_at,
            summary=summary,
        )

    def plans(self) -> DemoExecutionPlanList:
        plans = [self._to_plan(item) for item in self._risk.assessments().assessments]
        return DemoExecutionPlanList(count=len(plans), plans=plans)

    def trades(self) -> DemoTradeRecordList:
        trades = sorted(
            self._trades.values(),
            key=lambda item: (item.lifecycle is not DemoTradeLifecycle.OPEN, item.opened_at),
        )
        return DemoTradeRecordList(count=len(trades), trades=trades)

    def auto_execute_pending(self) -> int:
        """Best-effort auto-activate every currently executable plan once per cycle."""

        if self.status().state is not DemoExecutionState.READY:
            return 0

        activated = 0
        for plan in self.plans().plans:
            if plan.plan_state is not DemoPlanState.EXECUTABLE:
                continue
            if any(trade.signal_id == plan.signal_id for trade in self._trades.values()):
                continue
            try:
                self.activate(plan.signal_id)
            except AppError:
                continue
            activated += 1
        return activated

    def account(self) -> DemoExecutionAccountResponse:
        client = self._required_client()
        try:
            account_payload = client.account()
            positions_payload = client.positions()
        except BinanceDemoPrivateClientError as exc:
            raise self._private_api_error(exc) from exc

        balances = []
        for asset in account_payload.get("assets", []):
            if not isinstance(asset, dict):
                continue
            asset_code = asset.get("asset")
            if not isinstance(asset_code, str):
                continue
            wallet_balance = self._decimal(asset.get("walletBalance", "0"), "walletBalance")
            available_balance = self._decimal(
                asset.get("availableBalance", "0"), "availableBalance"
            )
            unrealized_pnl = self._decimal(
                asset.get("unrealizedProfit", "0"), "unrealizedProfit"
            )
            if wallet_balance == 0 and available_balance == 0 and unrealized_pnl == 0:
                continue
            balances.append(
                DemoAccountBalance(
                    asset=asset_code,
                    wallet_balance=wallet_balance,
                    available_balance=available_balance,
                    unrealized_pnl=unrealized_pnl,
                )
            )

        positions = []
        for item in positions_payload:
            position_amount = self._decimal(item.get("positionAmt", "0"), "positionAmt")
            if position_amount == 0:
                continue
            positions.append(
                DemoPositionSnapshot(
                    symbol=str(item.get("symbol", "")),
                    side=(
                        ScannerDirection.LONG
                        if position_amount > 0
                        else ScannerDirection.SHORT
                    ),
                    quantity=abs(position_amount),
                    entry_price=self._decimal(item.get("entryPrice", "0"), "entryPrice"),
                    unrealized_pnl=self._decimal(
                        item.get("unRealizedProfit", "0"),
                        "unRealizedProfit",
                    ),
                )
            )

        return DemoExecutionAccountResponse(
            demo_private_execution_ready=True,
            can_trade=bool(account_payload.get("canTrade", False)),
            updated_at=datetime.now(UTC),
            total_wallet_balance_usdt=self._decimal(
                account_payload.get("totalWalletBalance", "0"),
                "totalWalletBalance",
            ),
            available_balance_usdt=self._decimal(
                account_payload.get("availableBalance", "0"),
                "availableBalance",
            ),
            total_unrealized_pnl_usdt=self._decimal(
                account_payload.get("totalUnrealizedProfit", "0"),
                "totalUnrealizedProfit",
            ),
            balances=balances,
            open_positions=positions,
        )

    def activate(
        self,
        signal_id: str,
        request: DemoExecutionActivateRequest | None = None,
    ) -> DemoTradeRecord:
        self._require_execution_unlocked()
        if request is not None and request.quantity is not None:
            raise AppError(
                status_code=409,
                code="CLIENT_QUANTITY_NOT_ALLOWED",
                message="Demo order quantity must come from the approved Risk assessment",
            )

        assessment = next(
            (
                item
                for item in self._risk.assessments().assessments
                if item.signal_id == signal_id
            ),
            None,
        )
        if assessment is None:
            raise AppError(
                status_code=404,
                code="SIGNAL_NOT_FOUND",
                message="No signal-derived demo execution plan was found",
            )
        if assessment.decision is not RiskDecision.APPROVED:
            raise AppError(
                status_code=409,
                code="PLAN_NOT_EXECUTABLE",
                message="Only an approved Risk assessment can open a demo trade",
            )
        if any(trade.signal_id == signal_id for trade in self._trades.values()):
            raise AppError(
                status_code=409,
                code="SIGNAL_ALREADY_EXECUTED",
                message="This Signal already has a tracked Demo execution",
            )
        if assessment.recommended_quantity is None:
            raise AppError(
                status_code=409,
                code="RISK_QUANTITY_UNAVAILABLE",
                message="Risk-approved quantity is unavailable",
            )
        if assessment.stop_loss_price is None:
            raise AppError(
                status_code=409,
                code="RISK_STOP_UNAVAILABLE",
                message="Risk-approved Stop Loss is unavailable",
            )

        client = self._required_client()
        try:
            mode = client.position_mode()
            if mode.get("dualSidePosition") is not False:
                raise AppError(
                    status_code=409,
                    code="HEDGE_MODE_UNSUPPORTED",
                    message="Demo execution requires Binance One-way position mode",
                )
            rules = parse_symbol_trading_rules(
                client.exchange_info(),
                symbol=assessment.symbol,
            )
            mark_price = self._mark_price(client.mark_price(assessment.symbol))
            quantity = rules.normalize_market_quantity(
                assessment.recommended_quantity
            )
            rules.validate_market_notional(quantity=quantity, mark_price=mark_price)
            stop_price = rules.normalize_protective_price(
                assessment.stop_loss_price,
                direction=assessment.direction,
                is_stop_loss=True,
            )
            self._validate_stop_side(
                direction=assessment.direction,
                entry_or_mark=mark_price,
                stop_price=stop_price,
            )
        except ExchangeRuleError as exc:
            raise AppError(
                status_code=409,
                code="EXCHANGE_RULE_VALIDATION_FAILED",
                message=str(exc),
            ) from exc
        except BinanceDemoPrivateClientError as exc:
            raise self._private_api_error(exc) from exc

        entry_client_id, stop_client_id, take_client_id, close_client_id = (
            self._client_order_ids(signal_id)
        )
        try:
            entry_payload = self._existing_or_new_entry(
                client=client,
                assessment=assessment,
                quantity=quantity,
                client_order_id=entry_client_id,
            )
        except BinanceDemoPrivateClientError as exc:
            raise self._private_api_error(exc, code="DEMO_ORDER_REJECTED") from exc

        try:
            entry_order_id, entry_price, executed_quantity = self._verified_filled_order(
                payload=entry_payload,
                expected_client_order_id=entry_client_id,
                maximum_quantity=quantity,
            )
        except AppError:
            partial_quantity = self._bounded_executed_quantity(
                entry_payload,
                maximum_quantity=quantity,
            )
            if partial_quantity > 0:
                self._emergency_close(
                    client=client,
                    assessment=assessment,
                    quantity=partial_quantity,
                    client_order_id=close_client_id,
                )
            raise

        stop_placed = False
        try:
            self._validate_stop_side(
                direction=assessment.direction,
                entry_or_mark=entry_price,
                stop_price=stop_price,
            )
            risk_distance = abs(entry_price - stop_price)
            if risk_distance <= 0:
                raise AppError(
                    status_code=409,
                    code="PROTECTIVE_PRICE_INVALID",
                    message="Verified fill does not leave a positive Stop Loss distance",
                )
            take_profit_raw = (
                entry_price
                + risk_distance * self._settings.execution_take_profit_r_multiple
                if assessment.direction is ScannerDirection.LONG
                else entry_price
                - risk_distance * self._settings.execution_take_profit_r_multiple
            )
            take_profit_price = rules.normalize_protective_price(
                take_profit_raw,
                direction=assessment.direction,
                is_stop_loss=False,
            )
            latest_mark = self._mark_price(client.mark_price(assessment.symbol))
            self._validate_protection_around_mark(
                direction=assessment.direction,
                mark_price=latest_mark,
                stop_price=stop_price,
                take_profit_price=take_profit_price,
            )

            exit_side = self._opposite_order_side(assessment.direction)
            stop_payload = client.place_protective_order(
                symbol=assessment.symbol,
                side=exit_side.value,
                order_type="STOP_MARKET",
                quantity=self._decimal_text(executed_quantity),
                stop_price=self._decimal_text(stop_price),
                new_client_order_id=stop_client_id,
            )
            stop_order_id = self._verified_protective_order(
                payload=stop_payload,
                expected_client_order_id=stop_client_id,
            )
            stop_placed = True
            take_payload = client.place_protective_order(
                symbol=assessment.symbol,
                side=exit_side.value,
                order_type="TAKE_PROFIT_MARKET",
                quantity=self._decimal_text(executed_quantity),
                stop_price=self._decimal_text(take_profit_price),
                new_client_order_id=take_client_id,
            )
            take_order_id = self._verified_protective_order(
                payload=take_payload,
                expected_client_order_id=take_client_id,
            )
        except (BinanceDemoPrivateClientError, ExchangeRuleError, AppError) as exc:
            if stop_placed:
                self._cancel_best_effort(
                    client,
                    symbol=assessment.symbol,
                    client_order_id=stop_client_id,
                )
            self._emergency_close(
                client=client,
                assessment=assessment,
                quantity=executed_quantity,
                client_order_id=close_client_id,
            )
            raise AppError(
                status_code=502,
                code="PROTECTIVE_ORDER_FAILED_POSITION_CLOSED",
                message="Protective orders failed; the Binance Demo position was closed",
            ) from exc

        now = datetime.now(UTC)
        tracked_margin = self._scaled_margin(
            assessment=assessment,
            executed_quantity=executed_quantity,
        )
        trade = DemoTradeRecord(
            trade_id=str(uuid4()),
            signal_id=assessment.signal_id,
            symbol=assessment.symbol,
            direction=assessment.direction,
            setup=assessment.setup,
            setup_name=assessment.setup_name,
            lifecycle=DemoTradeLifecycle.OPEN,
            protection_state=DemoProtectionState.PROTECTED,
            grade=assessment.grade,
            entry_price=entry_price,
            stop_loss_price=stop_price,
            take_profit_price=take_profit_price,
            exit_price=None,
            exchange_order_id=entry_order_id,
            client_order_id=entry_client_id,
            stop_order_id=stop_order_id,
            stop_client_order_id=stop_client_id,
            take_profit_order_id=take_order_id,
            take_profit_client_order_id=take_client_id,
            requested_quantity=quantity,
            executed_quantity=executed_quantity,
            order_status="FILLED",
            tracked_margin_usdt=tracked_margin,
            unrealized_pnl_usdt=Decimal("0"),
            realized_pnl_usdt=Decimal("0"),
            opened_at=now,
            closed_at=None,
            closed_reason=None,
            updated_at=now,
        )
        self._trades[trade.trade_id] = trade
        self._persist_trades()
        return trade

    def get_trade(self, trade_id: str) -> DemoTradeRecord | None:
        """Return one tracked trade by identifier."""

        return self._trades.get(trade_id)

    def store_trade(self, trade: DemoTradeRecord) -> DemoTradeRecord:
        """Persist one updated tracked trade in process memory."""

        self._trades[trade.trade_id] = trade
        self._persist_trades()
        return trade

    def _load_trades(self) -> None:
        if self._trade_store_path is None or not self._trade_store_path.exists():
            return
        try:
            payload = json.loads(self._trade_store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _TRADE_STORE_LOGGER.warning(
                "Tracked demo trade store could not be loaded",
                extra={"path": str(self._trade_store_path), "error": str(exc)},
            )
            return

        raw_trades = payload.get("trades") if isinstance(payload, dict) else None
        if not isinstance(raw_trades, list):
            _TRADE_STORE_LOGGER.warning(
                "Tracked demo trade store payload is invalid",
                extra={"path": str(self._trade_store_path)},
            )
            return

        loaded: dict[str, DemoTradeRecord] = {}
        try:
            for item in raw_trades:
                trade = DemoTradeRecord.model_validate(item)
                loaded[trade.trade_id] = trade
        except ValidationError as exc:
            _TRADE_STORE_LOGGER.warning(
                "Tracked demo trade store validation failed",
                extra={"path": str(self._trade_store_path), "error": str(exc)},
            )
            return
        self._trades = loaded

    def _persist_trades(self) -> None:
        if self._trade_store_path is None:
            return
        payload = {
            "trades": [
                trade.model_dump(mode="json")
                for trade in self.trades().trades
            ]
        }
        try:
            self._trade_store_path.parent.mkdir(parents=True, exist_ok=True)
            self._trade_store_path.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            _TRADE_STORE_LOGGER.warning(
                "Tracked demo trade store could not be persisted",
                extra={"path": str(self._trade_store_path), "error": str(exc)},
            )

    def _existing_or_new_entry(
        self,
        *,
        client: ExecutionPrivateClient,
        assessment: RiskAssessment,
        quantity: Decimal,
        client_order_id: str,
    ) -> dict[str, Any]:
        try:
            existing = client.query_order(
                symbol=assessment.symbol,
                orig_client_order_id=client_order_id,
            )
        except BinanceDemoPrivateClientError as exc:
            if exc.exchange_code != _ORDER_NOT_FOUND:
                raise
        else:
            return existing
        try:
            return client.place_market_order(
                symbol=assessment.symbol,
                side=self._order_side_for_direction(assessment.direction).value,
                quantity=self._decimal_text(quantity),
                new_client_order_id=client_order_id,
            )
        except BinanceDemoPrivateClientError as submit_error:
            try:
                return client.query_order(
                    symbol=assessment.symbol,
                    orig_client_order_id=client_order_id,
                )
            except BinanceDemoPrivateClientError as query_error:
                if query_error.exchange_code == _ORDER_NOT_FOUND:
                    raise submit_error
                raise

    def _emergency_close(
        self,
        *,
        client: ExecutionPrivateClient,
        assessment: RiskAssessment,
        quantity: Decimal,
        client_order_id: str,
    ) -> None:
        try:
            payload = client.place_market_order(
                symbol=assessment.symbol,
                side=self._opposite_order_side(assessment.direction).value,
                quantity=self._decimal_text(quantity),
                new_client_order_id=client_order_id,
                reduce_only=True,
            )
            self._verified_filled_order(
                payload=payload,
                expected_client_order_id=client_order_id,
                maximum_quantity=quantity,
            )
        except (BinanceDemoPrivateClientError, AppError) as exc:
            raise AppError(
                status_code=503,
                code="UNPROTECTED_DEMO_POSITION",
                message="Protective orders failed and emergency Demo close was not verified",
            ) from exc

    @staticmethod
    def _cancel_best_effort(
        client: ExecutionPrivateClient,
        *,
        symbol: str,
        client_order_id: str,
    ) -> None:
        try:
            client.cancel_order(
                symbol=symbol,
                orig_client_order_id=client_order_id,
            )
        except BinanceDemoPrivateClientError:
            return

    @staticmethod
    def _bounded_executed_quantity(
        payload: dict[str, Any],
        *,
        maximum_quantity: Decimal,
    ) -> Decimal:
        try:
            quantity = Decimal(str(payload.get("executedQty", "0")))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal("0")
        if not quantity.is_finite() or quantity <= 0 or quantity > maximum_quantity:
            return Decimal("0")
        return quantity

    @staticmethod
    def _verified_filled_order(
        *,
        payload: dict[str, Any],
        expected_client_order_id: str,
        maximum_quantity: Decimal,
    ) -> tuple[str, Decimal, Decimal]:
        client_order_id = payload.get("clientOrderId")
        order_id = payload.get("orderId")
        if client_order_id != expected_client_order_id or order_id is None:
            raise AppError(
                status_code=502,
                code="ENTRY_ORDER_IDENTITY_INVALID",
                message="Binance Demo did not confirm the expected entry order identity",
            )
        if payload.get("status") != "FILLED":
            raise AppError(
                status_code=502,
                code="ENTRY_FILL_NOT_VERIFIED",
                message="Binance Demo entry order is not fully filled",
            )
        entry_price = DemoExecutionService._decimal(
            payload.get("avgPrice"),
            "avgPrice",
        )
        executed_quantity = DemoExecutionService._decimal(
            payload.get("executedQty"),
            "executedQty",
        )
        if entry_price <= 0 or executed_quantity <= 0:
            raise AppError(
                status_code=502,
                code="ENTRY_FILL_NOT_VERIFIED",
                message="Binance Demo fill price and quantity must be positive",
            )
        if executed_quantity > maximum_quantity:
            raise AppError(
                status_code=502,
                code="ENTRY_FILL_EXCEEDS_RISK_QUANTITY",
                message="Binance Demo executed more than the Risk-approved quantity",
            )
        return str(order_id), entry_price, executed_quantity

    @staticmethod
    def _verified_protective_order(
        *,
        payload: dict[str, Any],
        expected_client_order_id: str,
    ) -> str:
        client_order_id = payload.get("clientOrderId")
        order_id = payload.get("orderId")
        status = payload.get("status")
        if (
            client_order_id != expected_client_order_id
            or order_id is None
            or status not in _OPEN_PROTECTION_STATUSES
        ):
            raise AppError(
                status_code=502,
                code="PROTECTIVE_ORDER_NOT_VERIFIED",
                message="Binance Demo did not confirm an active protective order",
            )
        return str(order_id)

    @staticmethod
    def _validate_stop_side(
        *,
        direction: ScannerDirection,
        entry_or_mark: Decimal,
        stop_price: Decimal,
    ) -> None:
        invalid = (
            stop_price >= entry_or_mark
            if direction is ScannerDirection.LONG
            else stop_price <= entry_or_mark
        )
        if invalid:
            raise AppError(
                status_code=409,
                code="PROTECTIVE_PRICE_INVALID",
                message="Risk-approved Stop Loss is on the wrong side of the market",
            )

    @staticmethod
    def _validate_protection_around_mark(
        *,
        direction: ScannerDirection,
        mark_price: Decimal,
        stop_price: Decimal,
        take_profit_price: Decimal,
    ) -> None:
        if direction is ScannerDirection.LONG:
            valid = stop_price < mark_price < take_profit_price
        else:
            valid = take_profit_price < mark_price < stop_price
        if not valid:
            raise AppError(
                status_code=409,
                code="PROTECTIVE_PRICE_INVALID",
                message="Protective triggers would be immediately executable",
            )

    def _scaled_margin(
        self,
        *,
        assessment: RiskAssessment,
        executed_quantity: Decimal,
    ) -> Decimal:
        if (
            assessment.required_margin_usdt is None
            or assessment.recommended_quantity is None
            or assessment.recommended_quantity <= 0
        ):
            return Decimal("0")
        return (
            assessment.required_margin_usdt
            * executed_quantity
            / assessment.recommended_quantity
        )

    def _to_plan(self, assessment: RiskAssessment) -> DemoExecutionPlan:
        execution_enabled = bool(self._settings.execution_enabled)
        plan_state = DemoPlanState.TERMINAL
        executable_now = False
        blocked_reason = assessment.blocked_reason
        if assessment.decision is RiskDecision.APPROVED:
            if self._settings.execution_take_profit_r_multiple <= 0:
                plan_state = DemoPlanState.BLOCKED
                blocked_reason = "TAKE_PROFIT_POLICY_NOT_CONFIGURED"
            elif (
                execution_enabled
                and self._settings.demo_credentials_configured
                and self._private_client is not None
            ):
                plan_state = DemoPlanState.EXECUTABLE
                executable_now = True
            else:
                plan_state = DemoPlanState.BLOCKED
                blocked_reason = "EXECUTION_CONFIGURATION_LOCKED"
        elif assessment.decision is RiskDecision.WATCH:
            plan_state = DemoPlanState.WATCH
        elif assessment.decision is RiskDecision.BLOCKED:
            plan_state = DemoPlanState.BLOCKED

        return DemoExecutionPlan(
            signal_id=assessment.signal_id,
            symbol=assessment.symbol,
            direction=assessment.direction,
            setup=assessment.setup,
            setup_name=assessment.setup_name,
            signal_lifecycle=assessment.signal_lifecycle,
            risk_decision=assessment.decision,
            plan_state=plan_state,
            grade=assessment.grade,
            score=assessment.score,
            confidence=assessment.confidence,
            entry_trigger_price=assessment.entry_trigger_price,
            stop_loss_price=assessment.stop_loss_price,
            recommended_quantity=assessment.recommended_quantity,
            take_profit_r_multiple=self._settings.execution_take_profit_r_multiple,
            blocked_reason=blocked_reason,
            executable_now=executable_now,
            updated_at=assessment.updated_at,
            audit_codes=list(assessment.audit_codes),
        )

    def _state_from_risk(self, risk_state: RiskEngineState) -> DemoExecutionState:
        if risk_state is not RiskEngineState.READY:
            return DemoExecutionState.WAITING_FOR_RISK
        if (
            self._settings.execution_enabled
            and self._settings.demo_credentials_configured
            and self._settings.execution_take_profit_r_multiple > 0
            and self._private_client is not None
        ):
            return DemoExecutionState.READY
        return DemoExecutionState.EXECUTION_LOCKED

    def _require_execution_unlocked(self) -> None:
        if not self._settings.execution_enabled:
            raise AppError(
                status_code=409,
                code="EXECUTION_DISABLED",
                message="Demo execution remains configuration-locked",
            )
        if not self._settings.demo_credentials_configured:
            raise AppError(
                status_code=409,
                code="DEMO_CREDENTIALS_NOT_CONFIGURED",
                message="Binance Demo credentials are required before demo execution can start",
            )
        if self._settings.execution_take_profit_r_multiple <= 0:
            raise AppError(
                status_code=409,
                code="TAKE_PROFIT_POLICY_NOT_CONFIGURED",
                message="An explicit positive Take Profit R multiple is required",
            )

    def _required_client(self) -> ExecutionPrivateClient:
        if self._private_client is None:
            raise AppError(
                status_code=409,
                code="DEMO_PRIVATE_API_NOT_CONFIGURED",
                message="Binance demo private API is not configured",
            )
        return self._private_client

    @staticmethod
    def _client_order_ids(signal_id: str) -> tuple[str, str, str, str]:
        suffix = signal_id[:20]
        return (
            f"af-e-{suffix}",
            f"af-s-{suffix}",
            f"af-t-{suffix}",
            f"af-x-{suffix}",
        )

    @staticmethod
    def _order_side_for_direction(direction: ScannerDirection) -> DemoOrderSide:
        if direction is ScannerDirection.LONG:
            return DemoOrderSide.BUY
        return DemoOrderSide.SELL

    @staticmethod
    def _opposite_order_side(direction: ScannerDirection) -> DemoOrderSide:
        if direction is ScannerDirection.LONG:
            return DemoOrderSide.SELL
        return DemoOrderSide.BUY

    @staticmethod
    def _mark_price(payload: dict[str, Any]) -> Decimal:
        mark_price = DemoExecutionService._decimal(payload.get("markPrice"), "markPrice")
        if mark_price <= 0:
            raise ExchangeRuleError("Exchange mark price is unavailable")
        return mark_price

    @staticmethod
    def _decimal(value: Any, field: str) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise AppError(
                status_code=502,
                code="DEMO_EXCHANGE_PAYLOAD_INVALID",
                message=f"Binance Demo field {field} is invalid",
            ) from exc
        if not parsed.is_finite():
            raise AppError(
                status_code=502,
                code="DEMO_EXCHANGE_PAYLOAD_INVALID",
                message=f"Binance Demo field {field} is invalid",
            )
        return parsed

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        return format(value, "f")

    @staticmethod
    def _private_api_error(
        exc: BinanceDemoPrivateClientError,
        *,
        code: str = "DEMO_PRIVATE_API_UNAVAILABLE",
    ) -> AppError:
        return AppError(
            status_code=502,
            code=code,
            message=str(exc),
        )
