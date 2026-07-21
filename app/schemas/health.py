"""Foundation health and system-status schemas."""

from datetime import datetime
from typing import Literal

from app.schemas.common import RuntimeState, StrictResponseModel


class LiveResponse(StrictResponseModel):
    status: Literal["ok"]
    service: str
    version: str
    timestamp: datetime


class ReadyResponse(StrictResponseModel):
    status: Literal["ready"]
    service: str
    version: str
    execution_status: RuntimeState
    market_data_status: RuntimeState
    demo_account_status: RuntimeState
    timestamp: datetime


class SystemStatusResponse(StrictResponseModel):
    service: str
    version: str
    environment: str
    execution_enabled: bool
    market_data_status: RuntimeState
    demo_account_status: RuntimeState
    timestamp: datetime
