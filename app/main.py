"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.dependencies import (
    close_runtime_clients,
    configure_runtime_repositories,
    get_execution_leader_lease,
    get_execution_service,
    get_execution_worker,
    get_private_demo_client,
    get_recovery_gate,
    get_scanner_service,
    get_startup_recovery_coordinator,
)
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import AppError, register_exception_handlers
from app.core.logging import configure_logging
from app.core.security import MUTATION_OPENAPI_PATHS, MutationReplayGuard
from app.persistence import Persistence, TradingStateRepositories
from app.services.execution_leader_safety import validate_leader_or_fail_closed
from app.services.global_reconciliation import GlobalReconciliationSafetyService
from app.services.order_reconciliation import ContinuousOrderReconciliationService
from app.services.position_reconciliation import ContinuousPositionReconciliationService
from app.services.protective_lifecycle import ProtectiveLifecycleVerificationService
from app.services.restart_recovery import RestartRecoveryOwnershipService

mutation_logger = logging.getLogger("astraforge.mutation_audit")
execution_logger = logging.getLogger("astraforge.execution")
recovery_logger = logging.getLogger("astraforge.recovery")
_EXECUTION_LEADER_VALIDATION_INTERVAL_SECONDS = 1.0
ASTRAFORGE_VERCEL_ORIGIN_REGEX = (
    r"^https://astra-forge-crypto-frontend(?:-[a-z0-9-]+)?\.vercel\.app$"
)


def _audit_mutation(request: Request, *, request_id: str, status_code: int) -> None:
    audit = getattr(request.state, "mutation_audit", None)
    if not isinstance(audit, dict):
        return
    outcome = "success" if status_code < 400 else "rejected" if status_code < 500 else "failed"
    mutation_logger.info(
        "Mutation request audited",
        extra={
            "request_id": request_id,
            **audit,
            "outcome": outcome,
            "status_code": status_code,
        },
    )


def _configure_mutation_openapi(application: FastAPI, *, api_prefix: str) -> None:
    """Publish the runtime-required Idempotency-Key contract accurately."""

    original_openapi = application.openapi

    def custom_openapi() -> dict[str, Any]:
        schema = original_openapi()
        paths = schema.get("paths")
        if not isinstance(paths, dict):
            return schema

        for relative_path in MUTATION_OPENAPI_PATHS:
            path_item = paths.get(f"{api_prefix}{relative_path}")
            if not isinstance(path_item, dict):
                continue
            operation = path_item.get("post")
            if not isinstance(operation, dict):
                continue
            parameters = operation.get("parameters")
            if not isinstance(parameters, list):
                continue
            for parameter in parameters:
                if not isinstance(parameter, dict):
                    continue
                name = parameter.get("name")
                location = parameter.get("in")
                if (
                    isinstance(name, str)
                    and name.lower() == "idempotency-key"
                    and location == "header"
                ):
                    parameter["required"] = True
                    parameter_schema = parameter.get("schema")
                    if isinstance(parameter_schema, dict):
                        parameter_schema["minLength"] = 16
                        parameter_schema["maxLength"] = 128
                        parameter_schema["pattern"] = r"^[A-Za-z0-9._:-]{16,128}$"
        return schema

    application.openapi = custom_openapi  # type: ignore[method-assign]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the AstraForge FastAPI application."""

    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings)
    persistence = Persistence.from_settings(resolved_settings)
    repositories = TradingStateRepositories(persistence) if persistence is not None else None
    configure_runtime_repositories(repositories)

    async def execution_worker_loop() -> None:
        worker = get_execution_worker()
        while True:
            try:
                processed = await asyncio.to_thread(worker.process_one)
                if processed:
                    execution_logger.info(
                        "Single execution worker completed a protected Demo command",
                        extra={"processed_count": processed},
                    )
            except AppError as exc:
                execution_logger.warning(
                    "Execution worker cycle failed closed",
                    extra={"code": exc.code, "message": str(exc)},
                )
            except Exception:
                execution_logger.exception("Unexpected execution worker cycle failure")
            await asyncio.sleep(5)

    async def execution_leader_validation_loop() -> None:
        gate = get_recovery_gate()
        lease = get_execution_leader_lease()
        while gate.snapshot().automation_ready:
            if not validate_leader_or_fail_closed(gate, lease):
                recovery = gate.snapshot()
                recovery_logger.critical(
                    "Execution leader ownership lost; automation failed closed",
                    extra={
                        "recovery_state": recovery.recovery_state.value,
                        "recovery_error": recovery.recovery_error,
                    },
                )
                return
            await asyncio.sleep(_EXECUTION_LEADER_VALIDATION_INTERVAL_SECONDS)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        startup_tasks: list[asyncio.Task[None]] = []
        recovery_coordinator = None
        if persistence is not None:
            persistence.initialize()
            application.state.persistence = persistence
            application.state.trading_state_repositories = repositories
        else:
            application.state.persistence = None
            application.state.trading_state_repositories = None

        gate = get_recovery_gate()
        application.state.recovery_gate = gate

        if resolved_settings.execution_enabled:
            recovery_coordinator = get_startup_recovery_coordinator()
            recovered = await recovery_coordinator.recover()
            recovery = gate.snapshot()
            if recovered:
                recovery_logger.info(
                    "Startup recovery completed; global reconciliation is required",
                    extra={"recovery_state": recovery.recovery_state.value},
                )
            else:
                recovery_logger.error(
                    "Startup recovery failed closed; automated Demo execution remains locked",
                    extra={
                        "recovery_state": recovery.recovery_state.value,
                        "recovery_error": recovery.recovery_error,
                    },
                )

        execution_service = get_execution_service()
        private_client = get_private_demo_client()
        protective_service = ProtectiveLifecycleVerificationService(
            execution_service,
            private_client,
            repositories,
            gate,
        )
        order_service = ContinuousOrderReconciliationService(
            execution_service,
            private_client,
            gate,
        )
        position_service = ContinuousPositionReconciliationService(
            execution_service,
            private_client,
            gate,
        )
        restart_service = RestartRecoveryOwnershipService(
            execution_service,
            private_client,
            gate,
        )
        global_service = GlobalReconciliationSafetyService(
            order_service,
            position_service,
            restart_service,
            gate,
            protective_service=protective_service,
        )
        application.state.protective_lifecycle_service = protective_service
        application.state.order_reconciliation_service = order_service
        application.state.position_reconciliation_service = position_service
        application.state.restart_recovery_service = restart_service
        application.state.global_reconciliation_service = global_service

        if resolved_settings.scanner_auto_start:
            await get_scanner_service().start(source="lifespan")

        if resolved_settings.execution_enabled and gate.snapshot().automation_ready:
            global_report = await asyncio.to_thread(global_service.reconcile)
            if global_report.blocking or not gate.snapshot().automation_ready:
                recovery_logger.error(
                    "Global reconciliation could not prove a safe exchange state",
                    extra={"error_codes": global_report.error_codes},
                )
            else:
                recovery_logger.info(
                    "Global reconciliation proved exchange safety; worker unlocked",
                    extra={"checked_at": global_report.checked_at.isoformat()},
                )
                startup_tasks.append(asyncio.create_task(global_service.run_forever()))
                startup_tasks.append(asyncio.create_task(execution_leader_validation_loop()))
                startup_tasks.append(asyncio.create_task(execution_worker_loop()))

        try:
            yield
        finally:
            for startup_task in startup_tasks:
                if not startup_task.done():
                    startup_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await startup_task
            with suppress(Exception):
                await get_scanner_service().stop()
            if recovery_coordinator is not None:
                recovery_coordinator.close()
            configure_runtime_repositories(None)
            await close_runtime_clients()
            if persistence is not None:
                persistence.close()

    application = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url=f"{resolved_settings.api_prefix}/openapi.json",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.mutation_replay_guard = MutationReplayGuard(
        ttl_seconds=resolved_settings.mutation_replay_ttl_seconds,
        cache_limit=resolved_settings.mutation_replay_cache_limit,
        repositories=repositories,
    )
    application.dependency_overrides[get_settings] = lambda: resolved_settings

    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_origin_regex=ASTRAFORGE_VERCEL_ORIGIN_REGEX,
        allow_credentials=resolved_settings.cors_allow_credentials,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-Request-ID",
        ],
    )

    @application.middleware("http")
    async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:
            _audit_mutation(request, request_id=request_id, status_code=500)
            raise
        response.headers["X-Request-ID"] = request_id
        _audit_mutation(
            request,
            request_id=request_id,
            status_code=response.status_code,
        )
        return response

    register_exception_handlers(application)
    application.include_router(api_router, prefix=resolved_settings.api_prefix)
    _configure_mutation_openapi(
        application,
        api_prefix=resolved_settings.api_prefix,
    )
    return application


app = create_app()
