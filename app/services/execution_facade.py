"""Read-compatible execution facade that denies new-entry writes outside the worker."""

from __future__ import annotations

from typing import Any

from app.core.errors import AppError
from app.schemas.execution import (
    DemoExecutionAccountResponse,
    DemoExecutionActivateRequest,
    DemoExecutionPlanList,
    DemoExecutionStatusResponse,
    DemoTradeRecord,
    DemoTradeRecordList,
)
from app.services.execution import DemoExecutionService
from app.services.execution_command import ExecutionCommandService


class WorkerIsolatedExecutionService(DemoExecutionService):
    """Expose reads/trade storage while making every new-entry path command-only."""

    def __init__(
        self,
        inner: DemoExecutionService,
        commands: ExecutionCommandService,
    ) -> None:
        self._inner = inner
        self._commands = commands

    def status(self) -> DemoExecutionStatusResponse:
        return self._inner.status()

    def plans(self) -> DemoExecutionPlanList:
        return self._inner.plans()

    def trades(self) -> DemoTradeRecordList:
        return self._inner.trades()

    def account(self) -> DemoExecutionAccountResponse:
        return self._inner.account()

    def auto_execute_pending(self) -> int:
        """Compatibility call queues durable commands and performs no Binance write."""

        return self._commands.enqueue_all_ready()

    def activate(
        self,
        signal_id: str,
        request: DemoExecutionActivateRequest | None = None,
    ) -> DemoTradeRecord:
        raise AppError(
            status_code=409,
            code="DIRECT_EXECUTION_FORBIDDEN",
            message=(
                "New Demo entries must be persisted as execution commands and processed "
                "only by the single execution worker"
            ),
        )

    def get_trade(self, trade_id: str) -> DemoTradeRecord | None:
        return self._inner.get_trade(trade_id)

    def store_trade(self, trade: DemoTradeRecord) -> DemoTradeRecord:
        return self._inner.store_trade(trade)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
