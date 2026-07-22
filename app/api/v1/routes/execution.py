"""Demo Execution Engine and durable command API routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import SecretStr

from app.api.v1.dependencies import (
    get_execution_command_service,
    get_execution_service,
    get_execution_worker,
    get_private_demo_client,
)
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.security import MutationAuthorization, authorize_mutation
from app.integrations.binance.private_demo_client import (
    BinanceDemoPrivateClient,
    BinanceDemoPrivateClientError,
)
from app.schemas.execution import (
    DemoAccountDiagnosticResponse,
    DemoExecutionAccountResponse,
    DemoExecutionActivateRequest,
    DemoExecutionPlanList,
    DemoExecutionStatusResponse,
    DemoPlanState,
    DemoTradeRecordList,
)
from app.schemas.execution_command import (
    ExecutionCommand,
    ExecutionCommandList,
    ExecutionCommandStatus,
    ExecutionCommandTransition,
    ExecutionWorkerStatus,
)
from app.schemas.scanner import ScannerDirection, ScannerGrade, ScannerSetup
from app.schemas.signals import SignalLifecycle
from app.services.execution import DemoExecutionService
from app.services.execution_command import ExecutionCommandService
from app.services.execution_worker import DemoExecutionWorker

router = APIRouter(prefix="/execution/demo", tags=["execution"])


def _secret_configured(value: SecretStr | None) -> bool:
    return bool(value and value.get_secret_value())


@router.get("/status", response_model=DemoExecutionStatusResponse)
async def execution_status(
    service: DemoExecutionService = Depends(get_execution_service),  # noqa: B008
) -> DemoExecutionStatusResponse:
    """Return current demo execution orchestration state."""

    return service.status()


@router.get("/account", response_model=DemoExecutionAccountResponse)
async def execution_account(
    service: DemoExecutionService = Depends(get_execution_service),  # noqa: B008
) -> DemoExecutionAccountResponse:
    """Return current Binance demo account and position snapshot."""

    return service.account()


@router.get(
    "/diagnostics/account",
    response_model=DemoAccountDiagnosticResponse,
    include_in_schema=False,
)
async def execution_account_diagnostic(
    settings: Settings = Depends(get_settings),  # noqa: B008
    client: BinanceDemoPrivateClient | None = Depends(get_private_demo_client),  # noqa: B008
) -> DemoAccountDiagnosticResponse:
    """Return a secret-safe diagnostic for Binance Demo account connectivity."""

    checked_at = datetime.now(UTC)
    api_key_configured = _secret_configured(settings.binance_demo_api_key)
    api_secret_configured = _secret_configured(settings.binance_demo_api_secret)
    base_url_host = (
        urlparse(settings.binance_demo_base_url).netloc
        if settings.binance_demo_base_url is not None
        else None
    )

    if client is None:
        missing = []
        if settings.binance_demo_base_url is None:
            missing.append("DEMO_BASE_URL_MISSING")
        if not api_key_configured:
            missing.append("DEMO_API_KEY_MISSING")
        if not api_secret_configured:
            missing.append("DEMO_API_SECRET_MISSING")
        error_code = "+".join(missing) or "DEMO_PRIVATE_API_NOT_CONFIGURED"
        return DemoAccountDiagnosticResponse(
            diagnostic_status="CONFIGURATION_LOCKED",
            demo_base_url_configured=settings.binance_demo_base_url is not None,
            demo_base_url_host=base_url_host,
            demo_api_key_configured=api_key_configured,
            demo_api_secret_configured=api_secret_configured,
            demo_credentials_configured=settings.demo_credentials_configured,
            private_client_available=False,
            execution_enabled=settings.execution_enabled,
            take_profit_r_multiple=settings.execution_take_profit_r_multiple,
            account_endpoint_status="NOT_TESTED",
            account_error_code=error_code,
            account_error_message=(
                "Demo private client is unavailable. Configure the Binance Demo base URL "
                "and both Demo API credential fields."
            ),
            checked_at=checked_at,
        )

    try:
        account_payload = client.account()
    except BinanceDemoPrivateClientError as exc:
        return DemoAccountDiagnosticResponse(
            diagnostic_status="ACCOUNT_API_ERROR",
            demo_base_url_configured=settings.binance_demo_base_url is not None,
            demo_base_url_host=base_url_host,
            demo_api_key_configured=api_key_configured,
            demo_api_secret_configured=api_secret_configured,
            demo_credentials_configured=settings.demo_credentials_configured,
            private_client_available=True,
            execution_enabled=settings.execution_enabled,
            take_profit_r_multiple=settings.execution_take_profit_r_multiple,
            account_endpoint_status="ERROR",
            account_error_code="DEMO_PRIVATE_ACCOUNT_REQUEST_FAILED",
            account_error_message=str(exc),
            account_error_status_code=exc.status_code,
            account_exchange_code=exc.exchange_code,
            checked_at=checked_at,
        )

    return DemoAccountDiagnosticResponse(
        diagnostic_status="CONNECTED",
        demo_base_url_configured=settings.binance_demo_base_url is not None,
        demo_base_url_host=base_url_host,
        demo_api_key_configured=api_key_configured,
        demo_api_secret_configured=api_secret_configured,
        demo_credentials_configured=settings.demo_credentials_configured,
        private_client_available=True,
        execution_enabled=settings.execution_enabled,
        take_profit_r_multiple=settings.execution_take_profit_r_multiple,
        account_endpoint_status="CONNECTED",
        account_can_trade=bool(account_payload.get("canTrade", False)),
        checked_at=checked_at,
    )


@router.get("/plans", response_model=DemoExecutionPlanList)
async def execution_plans(
    service: DemoExecutionService = Depends(get_execution_service),  # noqa: B008
    symbol: Annotated[str | None, Query()] = None,
    direction: Annotated[ScannerDirection | None, Query()] = None,
    setup: Annotated[ScannerSetup | None, Query()] = None,
    grade: Annotated[ScannerGrade | None, Query()] = None,
    lifecycle: Annotated[SignalLifecycle | None, Query()] = None,
    plan_state: Annotated[DemoPlanState | None, Query()] = None,
) -> DemoExecutionPlanList:
    """Return filtered demo execution plans."""

    normalized_symbol = symbol.strip().upper() if symbol is not None else None
    if normalized_symbol is not None and (
        not normalized_symbol or not normalized_symbol.isalnum()
    ):
        raise HTTPException(status_code=422, detail="Invalid symbol")
    plans = [
        plan
        for plan in service.plans().plans
        if (normalized_symbol is None or plan.symbol == normalized_symbol)
        and (direction is None or plan.direction is direction)
        and (setup is None or plan.setup is setup)
        and (grade is None or plan.grade is grade)
        and (lifecycle is None or plan.signal_lifecycle is lifecycle)
        and (plan_state is None or plan.plan_state is plan_state)
    ]
    return DemoExecutionPlanList(count=len(plans), plans=plans)


@router.get("/trades", response_model=DemoTradeRecordList)
async def execution_trades(
    service: DemoExecutionService = Depends(get_execution_service),  # noqa: B008
) -> DemoTradeRecordList:
    """Return tracked demo trades."""

    return service.trades()


@router.get("/commands/status", response_model=ExecutionCommandStatus)
async def execution_command_status(
    service: ExecutionCommandService = Depends(get_execution_command_service),  # noqa: B008
) -> ExecutionCommandStatus:
    return service.status()


@router.get("/commands", response_model=ExecutionCommandList)
async def execution_commands(
    service: ExecutionCommandService = Depends(get_execution_command_service),  # noqa: B008
) -> ExecutionCommandList:
    return service.list()


@router.get("/commands/{command_id}", response_model=ExecutionCommand)
async def execution_command_detail(
    command_id: Annotated[str, Path(min_length=64, max_length=64)],
    service: ExecutionCommandService = Depends(get_execution_command_service),  # noqa: B008
) -> ExecutionCommand:
    command = service.get(command_id)
    if command is None:
        raise HTTPException(status_code=404, detail="Execution command not found")
    return command


@router.get(
    "/commands/{command_id}/history",
    response_model=list[ExecutionCommandTransition],
)
async def execution_command_history(
    command_id: Annotated[str, Path(min_length=64, max_length=64)],
    service: ExecutionCommandService = Depends(get_execution_command_service),  # noqa: B008
) -> list[ExecutionCommandTransition]:
    if service.get(command_id) is None:
        raise HTTPException(status_code=404, detail="Execution command not found")
    return service.history(command_id)


@router.get("/worker/status", response_model=ExecutionWorkerStatus)
async def execution_worker_status(
    worker: DemoExecutionWorker = Depends(get_execution_worker),  # noqa: B008
) -> ExecutionWorkerStatus:
    return worker.status()


@router.post("/activate/{signal_id}", response_model=ExecutionCommand)
async def execution_activate(
    signal_id: Annotated[str, Path(min_length=64, max_length=64)],
    request: DemoExecutionActivateRequest | None = None,
    service: ExecutionCommandService = Depends(get_execution_command_service),  # noqa: B008
    _authorization: MutationAuthorization = Depends(authorize_mutation),  # noqa: B008
) -> ExecutionCommand:
    """Queue a durable command; this request path never submits a Binance order."""

    if request is not None and request.quantity is not None:
        raise AppError(
            status_code=409,
            code="CLIENT_QUANTITY_NOT_ALLOWED",
            message="Execution quantity must come from the current Risk approval",
        )
    return service.enqueue(signal_id)
