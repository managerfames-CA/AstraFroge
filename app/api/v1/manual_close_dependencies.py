"""Dependency factory for the durable manual-close mutation path."""

from __future__ import annotations

from typing import cast

from app.api.v1.dependencies import (
    get_execution_order_backend,
    get_execution_service,
    get_snapshot_private_client,
)
from app.persistence.repositories import TradingStateRepositories
from app.services.durable_trade_management import DurableTradeManagementService
from app.services.manual_close_durability import ManualCloseDurabilityService


def get_durable_trade_management_service() -> DurableTradeManagementService:
    """Build manual close over the current durable repository boundary."""

    backend = get_execution_order_backend()
    repositories = cast(
        TradingStateRepositories | None,
        getattr(backend, "_repositories", None),
    )
    durability = ManualCloseDurabilityService(repositories) if repositories is not None else None
    return DurableTradeManagementService(
        get_execution_service(),
        get_snapshot_private_client(),
        durability,
    )
