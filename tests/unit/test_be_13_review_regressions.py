"""Regression proofs for BE-13 automated review findings."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import httpx

from app.integrations.binance.recovery_demo_client import BinanceDemoRecoveryClient
from app.schemas.protective_lifecycle import (
    ProtectiveLifecycleFinding,
    ProtectiveLifecycleReport,
    ProtectiveLifecycleState,
)
from app.services.global_reconciliation import GlobalReconciliationSafetyService
from app.services.order_reconciliation import ContinuousOrderReconciliationService
from app.services.position_reconciliation import ContinuousPositionReconciliationService
from app.services.protective_lifecycle import ProtectiveLifecycleVerificationService
from app.services.recovery import AutomationRecoveryGate
from app.services.restart_recovery import RestartRecoveryOwnershipService
from tests.unit import test_be_07_global_reconciliation as be07

NOW = datetime(2026, 7, 19, 17, 30, tzinfo=UTC)


def test_native_algo_query_enriches_executed_quantity_from_actual_order() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/fapi/v1/algoOrder":
            return httpx.Response(
                200,
                json={
                    "clientAlgoId": "af-stop-1",
                    "algoId": "stop-algo-1",
                    "algoStatus": "PARTIALLY_FILLED",
                    "actualOrderId": "stop-actual-1",
                },
            )
        return httpx.Response(
            200,
            json={
                "orderId": "stop-actual-1",
                "executedQty": "0.04",
                "avgPrice": "99",
            },
        )

    client = BinanceDemoRecoveryClient(
        base_url="https://demo-fapi.binance.com",
        api_key="demo-key",
        api_secret="demo-secret",
        timeout_seconds=5,
        recv_window_ms=5000,
        transport=httpx.MockTransport(handler),
    )

    payload = client.query_algo_order(
        symbol="BTCUSDT",
        orig_client_order_id="af-stop-1",
    )

    assert requested_paths == ["/fapi/v1/algoOrder", "/fapi/v1/order"]
    assert payload["clientOrderId"] == "af-stop-1"
    assert payload["orderId"] == "stop-algo-1"
    assert payload["status"] == "PARTIALLY_FILLED"
    assert payload["actualOrderId"] == "stop-actual-1"
    assert payload["executedQty"] == "0.04"
    assert payload["avgPrice"] == "99"


class BlockingProtective:
    def __init__(self, gate: AutomationRecoveryGate) -> None:
        self.gate = gate
        self.calls = 0

    def reconcile(self) -> ProtectiveLifecycleReport:
        self.calls += 1
        self.gate.fail("PARTIAL_CLOSE_REQUIRES_PROTECTION_REVIEW")
        return ProtectiveLifecycleReport(
            state=ProtectiveLifecycleState.BLOCKED,
            checked_at=NOW,
            open_trade_count=1,
            verified_event_count=1 if self.calls == 1 else 0,
            partial_trade_count=1,
            closed_trade_count=0,
            blocking=True,
            findings=[
                ProtectiveLifecycleFinding(
                    code="PARTIAL_CLOSE_REQUIRES_PROTECTION_REVIEW",
                    message="Remaining protection requires review",
                    trade_id="trade-be13",
                    symbol="BTCUSDT",
                )
            ],
            events=[],
        )


async def _run_monitor_until_protective_rechecks() -> int:
    gate = be07._ready_gate()
    protective = BlockingProtective(gate)
    service = GlobalReconciliationSafetyService(
        cast(
            ContinuousOrderReconciliationService,
            be07._Order(be07._order()),
        ),
        cast(
            ContinuousPositionReconciliationService,
            be07._Position(be07._position()),
        ),
        cast(
            RestartRecoveryOwnershipService,
            be07._Restart(be07._restart()),
        ),
        gate,
        protective_service=cast(ProtectiveLifecycleVerificationService, protective),
        interval_seconds=0.001,
        now_provider=lambda: NOW,
    )
    task = asyncio.create_task(service.run_forever())
    for _ in range(100):
        if protective.calls >= 2:
            break
        await asyncio.sleep(0.001)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return protective.calls


def test_global_monitor_keeps_protective_observation_after_gate_blocks() -> None:
    assert asyncio.run(_run_monitor_until_protective_rechecks()) >= 2
