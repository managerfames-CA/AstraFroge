"""Dependency factory for exchange-authoritative Active Trades reads."""

from app.api.v1.dependencies import (
    get_account_snapshot_service,
    get_trade_management_service,
)
from app.services.active_trade_authority import ActiveTradeAuthorityService


def get_active_trade_authority_service() -> ActiveTradeAuthorityService:
    """Build a read authority over cached app-scoped trade and snapshot services."""

    return ActiveTradeAuthorityService(
        get_trade_management_service(),
        get_account_snapshot_service(),
    )
