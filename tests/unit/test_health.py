"""Health endpoint contract tests."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_frontend_compatible_health_path_returns_readiness(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["service"] == "AstraForge Crypto Backend"
    assert payload["execution_status"] == "blocked"


def test_explicit_ready_health_path_still_returns_readiness(client: TestClient) -> None:
    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["execution_status"] == "blocked"
