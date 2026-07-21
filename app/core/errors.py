"""Central application error handling."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas.common import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Expected application error with a stable public code."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str | None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message, request_id=request_id))
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=headers,
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return _error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        request_id=_request_id(request),
        headers=exc.headers,
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    logger.info(
        "Request validation failed",
        extra={"request_id": _request_id(request), "validation_errors": exc.errors()},
    )
    return _error_response(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed",
        request_id=_request_id(request),
    )


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    messages = {
        404: "Resource not found",
        405: "Method not allowed",
    }
    return _error_response(
        status_code=exc.status_code,
        code="HTTP_ERROR",
        message=messages.get(exc.status_code, "HTTP request failed"),
        request_id=_request_id(request),
        headers=exc.headers,
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled application error",
        extra={"request_id": _request_id(request)},
    )
    return _error_response(
        status_code=500,
        code="INTERNAL_ERROR",
        message="An internal error occurred",
        request_id=_request_id(request),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register all central exception handlers."""

    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)


def public_error_schema() -> dict[str, Any]:
    """Return the OpenAPI schema reference for standard error responses."""

    return {"model": ErrorResponse}
