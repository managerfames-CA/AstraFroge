"""Version 1 API router."""

from fastapi import APIRouter

from app.api.v1.routes import (
    execution,
    global_reconciliation,
    health,
    indicators,
    journal_performance,
    lifecycle_reconciliation,
    market,
    notifications,
    order_audit,
    order_reconciliation,
    position_reconciliation,
    protective_lifecycle,
    restart_recovery,
    risk,
    scanner,
    signals,
    system,
    trade_management,
    universe,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(system.router)
api_router.include_router(market.router)
api_router.include_router(universe.router)
api_router.include_router(indicators.router)
api_router.include_router(scanner.router)
api_router.include_router(signals.router)
api_router.include_router(risk.router)
api_router.include_router(execution.router)
api_router.include_router(trade_management.router)
api_router.include_router(protective_lifecycle.router)
api_router.include_router(order_audit.router)
api_router.include_router(order_reconciliation.router)
api_router.include_router(position_reconciliation.router)
api_router.include_router(lifecycle_reconciliation.router)
api_router.include_router(restart_recovery.router)
api_router.include_router(global_reconciliation.router)
api_router.include_router(journal_performance.router)
api_router.include_router(notifications.router)
