"""Continuous read-only reconciliation of Binance Demo positions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Any, Protocol

from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.schemas.execution import DemoTradeLifecycle, DemoTradeRecord, DemoTradeRecordList
from app.schemas.position_reconciliation import (
    PositionReconciliationFinding,
    PositionReconciliationReport,
    PositionReconciliationState,
)
from app.schemas.scanner import ScannerDirection
from app.services.recovery import AutomationRecoveryGate


class PositionReconciliationClient(Protocol):
    """Read-only Binance Demo position surface."""

    def positions(self) -> list[dict[str, Any]]: ...


class PositionTradeSource(Protocol):
    """Durable open-trade source used for comparison."""

    def trades(self) -> DemoTradeRecordList: ...


class ContinuousPositionReconciliationService:
    """Compare durable open trades with Binance Demo position truth."""

    def __init__(
        self,
        trade_source: PositionTradeSource,
        client: PositionReconciliationClient | None,
        recovery_gate: AutomationRecoveryGate,
        *,
        interval_seconds: float = 15.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Reconciliation interval must be positive")
        self._trade_source = trade_source
        self._client = client
        self._gate = recovery_gate
        self._interval_seconds = interval_seconds
        self._now = now_provider or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._latest = PositionReconciliationReport(
            state=PositionReconciliationState.NOT_RUN,
            checked_at=self._now(),
            local_open_trade_count=0,
            exchange_open_position_count=0,
            blocking=False,
            findings=[],
        )

    def latest(self) -> PositionReconciliationReport:
        """Return the latest completed position reconciliation report."""

        with self._lock:
            return self._latest.model_copy(deep=True)

    async def run_forever(self) -> None:
        """Run while automation remains ready, stopping on blocking drift."""

        while True:
            if self._gate.snapshot().automation_ready:
                report = await asyncio.to_thread(self.reconcile)
                if report.blocking:
                    return
            await asyncio.sleep(self._interval_seconds)

    def reconcile(self) -> PositionReconciliationReport:
        """Run one read-only position reconciliation cycle."""

        checked_at = self._now()
        trades = [
            trade
            for trade in self._trade_source.trades().trades
            if trade.lifecycle is DemoTradeLifecycle.OPEN
        ]
        client = self._client
        if client is None:
            return self._publish_unavailable(
                checked_at,
                len(trades),
                "DEMO_PRIVATE_API_NOT_CONFIGURED",
                "Binance Demo private API is unavailable for position reconciliation",
            )

        try:
            positions = client.positions()
            findings, open_position_count = self._compare(trades, positions)
        except BinanceDemoPrivateClientError:
            return self._publish_unavailable(
                checked_at,
                len(trades),
                "POSITION_RECONCILIATION_UNAVAILABLE",
                "Binance Demo position reconciliation request failed",
            )
        except Exception:
            return self._publish_unavailable(
                checked_at,
                len(trades),
                "POSITION_RECONCILIATION_INVALID",
                "Position reconciliation could not prove a safe exchange state",
            )

        blocking = any(item.blocking for item in findings)
        report = PositionReconciliationReport(
            state=(
                PositionReconciliationState.DRIFT_DETECTED
                if blocking
                else PositionReconciliationState.IN_SYNC
            ),
            checked_at=checked_at,
            local_open_trade_count=len(trades),
            exchange_open_position_count=open_position_count,
            blocking=blocking,
            findings=findings,
        )
        self._publish(report)
        if blocking:
            self._gate.fail("CONTINUOUS_POSITION_RECONCILIATION_DRIFT")
        return report

    def _compare(
        self,
        trades: list[DemoTradeRecord],
        positions: list[dict[str, Any]],
    ) -> tuple[list[PositionReconciliationFinding], int]:
        findings: list[PositionReconciliationFinding] = []
        local_by_symbol: dict[str, DemoTradeRecord] = {}

        for trade in trades:
            previous = local_by_symbol.get(trade.symbol)
            if previous is not None:
                findings.append(
                    PositionReconciliationFinding(
                        code="DUPLICATE_LOCAL_OPEN_POSITION",
                        message="More than one durable open trade exists for the symbol",
                        symbol=trade.symbol,
                        trade_id=trade.trade_id,
                        expected_direction=trade.direction,
                        expected_quantity=trade.executed_quantity,
                    )
                )
                continue
            local_by_symbol[trade.symbol] = trade

        exchange_by_symbol: dict[str, tuple[ScannerDirection, Decimal]] = {}
        for payload in positions:
            symbol = self._text(payload.get("symbol"))
            if symbol is None:
                findings.append(
                    PositionReconciliationFinding(
                        code="EXCHANGE_POSITION_PAYLOAD_INVALID",
                        message="An exchange position has no symbol",
                    )
                )
                continue
            try:
                signed_quantity = self._decimal(payload.get("positionAmt"))
            except ValueError:
                findings.append(
                    PositionReconciliationFinding(
                        code="EXCHANGE_POSITION_PAYLOAD_INVALID",
                        message="An exchange position quantity is invalid",
                        symbol=symbol,
                    )
                )
                continue
            if signed_quantity == 0:
                continue
            if symbol in exchange_by_symbol:
                findings.append(
                    PositionReconciliationFinding(
                        code="DUPLICATE_EXCHANGE_OPEN_POSITION",
                        message="Binance Demo returned duplicate non-zero positions",
                        symbol=symbol,
                        actual_quantity=abs(signed_quantity),
                    )
                )
                continue
            exchange_by_symbol[symbol] = (
                ScannerDirection.LONG
                if signed_quantity > 0
                else ScannerDirection.SHORT,
                abs(signed_quantity),
            )

        for symbol, trade in local_by_symbol.items():
            actual = exchange_by_symbol.get(symbol)
            if actual is None:
                findings.append(
                    PositionReconciliationFinding(
                        code="EXTERNAL_POSITION_CLOSE_DETECTED",
                        message=(
                            "A durable open trade has no Binance Demo position; "
                            "an external close or untracked lifecycle event is possible"
                        ),
                        symbol=symbol,
                        trade_id=trade.trade_id,
                        expected_direction=trade.direction,
                        expected_quantity=trade.executed_quantity,
                    )
                )
                continue
            actual_direction, actual_quantity = actual
            if actual_direction is not trade.direction:
                findings.append(
                    PositionReconciliationFinding(
                        code="EXCHANGE_POSITION_DIRECTION_MISMATCH",
                        message="Exchange position direction differs from durable state",
                        symbol=symbol,
                        trade_id=trade.trade_id,
                        expected_direction=trade.direction,
                        actual_direction=actual_direction,
                        expected_quantity=trade.executed_quantity,
                        actual_quantity=actual_quantity,
                    )
                )
            if actual_quantity != trade.executed_quantity:
                findings.append(
                    PositionReconciliationFinding(
                        code="EXCHANGE_POSITION_QUANTITY_MISMATCH",
                        message="Exchange position quantity differs from durable state",
                        symbol=symbol,
                        trade_id=trade.trade_id,
                        expected_direction=trade.direction,
                        actual_direction=actual_direction,
                        expected_quantity=trade.executed_quantity,
                        actual_quantity=actual_quantity,
                    )
                )

        for symbol in sorted(set(exchange_by_symbol) - set(local_by_symbol)):
            actual_direction, actual_quantity = exchange_by_symbol[symbol]
            findings.append(
                PositionReconciliationFinding(
                    code="ORPHAN_EXCHANGE_POSITION",
                    message=(
                        "Binance Demo has an open position with no durable tracked trade"
                    ),
                    symbol=symbol,
                    actual_direction=actual_direction,
                    actual_quantity=actual_quantity,
                )
            )

        return findings, len(exchange_by_symbol)

    def _publish_unavailable(
        self,
        checked_at: datetime,
        local_count: int,
        code: str,
        message: str,
    ) -> PositionReconciliationReport:
        report = PositionReconciliationReport(
            state=PositionReconciliationState.UNAVAILABLE,
            checked_at=checked_at,
            local_open_trade_count=local_count,
            exchange_open_position_count=0,
            blocking=True,
            findings=[PositionReconciliationFinding(code=code, message=message)],
        )
        self._publish(report)
        self._gate.fail(code)
        return report

    def _publish(self, report: PositionReconciliationReport) -> None:
        with self._lock:
            self._latest = report

    @staticmethod
    def _text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Invalid decimal") from exc
        if not parsed.is_finite():
            raise ValueError("Invalid decimal")
        return parsed
