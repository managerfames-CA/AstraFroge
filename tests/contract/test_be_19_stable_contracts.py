"""API contract stability tests for BE-19 status endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_signals_status_contract(client: TestClient) -> None:
    """Verify that signals status response matches the stable BE-19 contract."""
    response = client.get("/api/v1/signals/status")
    assert response.status_code == 200
    body = response.json()
    assert body["contract_version"] == "1"
    assert "execution_integration_available" in body
    assert isinstance(body["execution_integration_available"], bool)


def test_risk_status_contract(client: TestClient) -> None:
    """Verify that risk status response matches the stable BE-19 contract."""
    response = client.get("/api/v1/risk/status")
    assert response.status_code == 200
    body = response.json()
    assert body["contract_version"] == "1"
    assert "execution_integration_available" not in body


def test_execution_status_contract(client: TestClient) -> None:
    """Verify that execution status response matches the stable BE-19 contract."""
    response = client.get("/api/v1/execution/demo/status")
    assert response.status_code == 200
    body = response.json()
    assert body["contract_version"] == "1"
    assert "execution_integration_available" in body
    assert isinstance(body["execution_integration_available"], bool)


def test_trade_management_status_contract(client: TestClient) -> None:
    """Verify that trade management status response matches the stable BE-19 contract."""
    response = client.get("/api/v1/trade-management/status")
    assert response.status_code == 200
    body = response.json()
    assert body["contract_version"] == "1"
    assert "execution_integration_available" not in body


def test_journal_performance_status_contract(client: TestClient) -> None:
    """Verify that journal performance status response matches the stable BE-19 contract."""
    response = client.get("/api/v1/journal-performance/status")
    assert response.status_code == 200
    body = response.json()
    assert body["contract_version"] == "1"
    assert "execution_integration_available" not in body


def test_openapi_publishes_be19_versioned_contracts(client: TestClient) -> None:
    """Verify that OpenAPI specification correctly publishes versioned status properties."""
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    components = schema.get("components", {}).get("schemas", {})

    status_schemas = [
        "SignalStatusResponse",
        "RiskStatusResponse",
        "DemoExecutionStatusResponse",
        "TradeManagementStatusResponse",
        "JournalPerformanceStatusResponse",
    ]

    for model_name in status_schemas:
        assert model_name in components, f"Missing component schema {model_name}"
        properties = components[model_name].get("properties", {})
        assert "contract_version" in properties, f"contract_version missing in {model_name}"
        if model_name in {"SignalStatusResponse", "DemoExecutionStatusResponse"}:
            assert "execution_integration_available" in properties, (
                f"execution_integration_available missing in {model_name}"
            )
        else:
            assert "execution_integration_available" not in properties, (
                f"execution_integration_available should not be in {model_name}"
            )
