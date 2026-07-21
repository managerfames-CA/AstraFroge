"""Shared API schemas."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class RuntimeState(StrEnum):
    """Honest runtime states for unavailable or unfinished capabilities."""

    NOT_CONFIGURED = "not_configured"
    NOT_CONNECTED = "not_connected"
    UNAVAILABLE = "unavailable"
    EMPTY = "empty"
    BLOCKED = "blocked"
    DEGRADED = "degraded"
    ERROR = "error"


class StrictResponseModel(BaseModel):
    """Base response model that rejects accidental undocumented fields."""

    model_config = ConfigDict(extra="forbid")


class ErrorDetail(StrictResponseModel):
    code: str
    message: str
    request_id: str | None = None


class ErrorResponse(StrictResponseModel):
    error: ErrorDetail
