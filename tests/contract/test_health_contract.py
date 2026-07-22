"""API contract tests for verified runtime endpoints."""

from fastapi.testclient import TestClient


def test_live_contract(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "AstraForge Crypto Backend"
    assert body["version"] == "0.4.0"
    assert body["timestamp"].endswith("Z")
    assert response.headers["X-Request-ID"]


def test_ready_contract_is_honest(client: TestClient) -> None:
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "service": "AstraForge Crypto Backend",
        "version": "0.4.0",
        "execution_status": "blocked",
        "market_data_status": "not_configured",
        "demo_account_status": "not_configured",
        "timestamp": response.json()["timestamp"],
    }


def test_system_status_contract_is_fail_closed(client: TestClient) -> None:
    response = client.get("/api/v1/system/status")
    assert response.status_code == 200
    body = response.json()
    assert body["environment"] == "test"
    assert body["execution_enabled"] is False
    assert body["market_data_status"] == "not_configured"
    assert body["demo_account_status"] == "not_configured"
    assert "balance" not in body
    assert "position" not in body
    assert "pnl" not in body


def test_protective_lifecycle_contract_starts_not_run(client: TestClient) -> None:
    response = client.get("/api/v1/protective-lifecycle/status")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "NOT_RUN"
    assert body["verified_event_count"] == 0
    assert body["events"] == []


def test_order_audit_contract_is_truthfully_unavailable(client: TestClient) -> None:
    response = client.get("/api/v1/order-audit/status")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "UNAVAILABLE"
    assert body["blocking"] is True
    assert body["audited_order_count"] == 0
    assert body["findings"][0]["code"] == "ORDER_AUDIT_DURABILITY_UNAVAILABLE"


def test_lifecycle_reconciliation_contract_is_fail_closed(client: TestClient) -> None:
    response = client.get("/api/v1/lifecycle-reconciliation/status")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "UNAVAILABLE"
    assert body["blocking"] is True
    assert body["finding_count"] == 2
    assert {item["code"] for item in body["findings"]} == {
        "ORDER_RECONCILIATION_NOT_READY",
        "POSITION_RECONCILIATION_NOT_READY",
    }


def test_restart_recovery_contract_is_fail_closed_before_reconciliation(client: TestClient) -> None:
    response = client.get("/api/v1/restart-recovery/status")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "NOT_READY"
    assert body["blocking"] is True
    assert body["exchange_reconciled"] is False
    assert body["error"] == "STARTUP_EXCHANGE_RECONCILIATION_NOT_COMPLETE"


def test_global_reconciliation_contract_starts_fail_closed(client: TestClient) -> None:
    response = client.get("/api/v1/global-reconciliation/status")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "NOT_RUN"
    assert body["blocking"] is True
    assert body["automation_ready"] is False
    assert body["error_codes"] == ["GLOBAL_RECONCILIATION_NOT_RUN"]


def test_openapi_contains_verified_runtime_routes(client: TestClient) -> None:
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    paths = set(response.json()["paths"])
    assert paths == {
        "/api/v1/global-reconciliation/status",
        "/api/v1/health",
        "/api/v1/health/live",
        "/api/v1/health/ready",
        "/api/v1/indicators/{symbol}",
        "/api/v1/journal-performance/journal",
        "/api/v1/journal-performance/performance",
        "/api/v1/journal-performance/reports",
        "/api/v1/journal-performance/status",
        "/api/v1/lifecycle-reconciliation/status",
        "/api/v1/market/klines/{symbol}",
        "/api/v1/market/status",
        "/api/v1/market/symbols",
        "/api/v1/market/ticker/{symbol}",
        "/api/v1/execution/demo/activate/{signal_id}",
        "/api/v1/execution/demo/account",
        "/api/v1/execution/demo/commands",
        "/api/v1/execution/demo/commands/status",
        "/api/v1/execution/demo/commands/{command_id}",
        "/api/v1/execution/demo/commands/{command_id}/history",
        "/api/v1/execution/demo/plans",
        "/api/v1/execution/demo/status",
        "/api/v1/execution/demo/trades",
        "/api/v1/execution/demo/worker/status",
        "/api/v1/notifications",
        "/api/v1/notifications/status",
        "/api/v1/notifications/{notification_id}",
        "/api/v1/notifications/{notification_id}/read",
        "/api/v1/order-audit/orders",
        "/api/v1/order-audit/status",
        "/api/v1/order-reconciliation/status",
        "/api/v1/position-reconciliation/status",
        "/api/v1/protective-lifecycle/status",
        "/api/v1/restart-recovery/status",
        "/api/v1/risk/assessments",
        "/api/v1/risk/status",
        "/api/v1/scanner/candidates",
        "/api/v1/scanner/run-now",
        "/api/v1/scanner/runs/latest",
        "/api/v1/scanner/start",
        "/api/v1/scanner/status",
        "/api/v1/scanner/stop",
        "/api/v1/signals",
        "/api/v1/signals/{signal_id}",
        "/api/v1/signals/status",
        "/api/v1/system/status",
        "/api/v1/trade-management/close/{trade_id}",
        "/api/v1/trade-management/status",
        "/api/v1/trade-management/trades",
        "/api/v1/universe",
    }
