"""Exchange-authoritative durable order audit observability."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.v1.dependencies import (
    get_execution_service,
    get_private_demo_client,
    get_recovery_gate,
)
from app.persistence.repositories import TradingStateRepositories
from app.schemas.order_audit import (
    OrderAuditRecordList,
    OrderAuditState,
    OrderAuditStatusResponse,
)
from app.services.global_reconciliation import GlobalReconciliationSafetyService
from app.services.order_audit_runtime import RuntimeOrderAuditService

router = APIRouter(prefix="/order-audit", tags=["order-audit"])


def _service(request: Request) -> RuntimeOrderAuditService:
    service = getattr(request.app.state, "order_audit_service", None)
    if isinstance(service, RuntimeOrderAuditService):
        return service

    global_service = getattr(request.app.state, "global_reconciliation_service", None)
    if isinstance(global_service, GlobalReconciliationSafetyService):
        startup_service = global_service.order_audit_service()
        if startup_service is not None:
            request.app.state.order_audit_service = startup_service
            return startup_service

    repositories = getattr(request.app.state, "trading_state_repositories", None)
    if not isinstance(repositories, TradingStateRepositories):
        repositories = None
    fallback = RuntimeOrderAuditService(
        get_execution_service(),
        get_private_demo_client(),
        repositories,
        get_recovery_gate(),
    )
    request.app.state.order_audit_service = fallback
    return fallback


@router.get("/status", response_model=OrderAuditStatusResponse)
def order_audit_status(request: Request) -> OrderAuditStatusResponse:
    """Refresh read-only Binance evidence and return the latest audit state."""

    return _service(request).reconcile()


@router.get("/orders", response_model=OrderAuditRecordList)
def order_audit_orders(request: Request) -> OrderAuditRecordList:
    """Return records without hiding a failed or unavailable reconciliation."""

    service = _service(request)
    report = service.reconcile()
    records = service.records()
    if report.state is OrderAuditState.READY and not report.blocking:
        return records
    return records.model_copy(
        update={
            "state": report.state,
            "blocking": True,
            "findings": [*report.findings, *records.findings],
        }
    )
