"""Integration tests for error handling and CORS."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import AppError
from app.main import create_app


def _test_app() -> FastAPI:
    settings = Settings(
        _env_file=None,
        environment="test",
        cors_origins=["https://approved.example"],
        binance_demo_api_key="configured-key",
        binance_demo_api_secret="configured-secret",
    )
    app = create_app(settings)

    @app.get("/expected-error")
    def expected_error() -> None:
        raise AppError(status_code=409, code="TEST_CONFLICT", message="Safe conflict")

    @app.get("/unexpected-error")
    def unexpected_error() -> None:
        raise RuntimeError("configured-secret must never leak")

    return app


def test_expected_error_uses_stable_envelope() -> None:
    with TestClient(_test_app(), raise_server_exceptions=False) as client:
        response = client.get("/expected-error", headers={"X-Request-ID": "request-123"})

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "TEST_CONFLICT",
            "message": "Safe conflict",
            "request_id": "request-123",
        }
    }


def test_unhandled_error_does_not_expose_exception_or_secret() -> None:
    with TestClient(_test_app(), raise_server_exceptions=False) as client:
        response = client.get("/unexpected-error")

    assert response.status_code == 500
    body = response.text
    assert "configured-secret" not in body
    assert "RuntimeError" not in body
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


def test_cors_allows_only_approved_origin() -> None:
    with TestClient(_test_app()) as client:
        allowed = client.options(
            "/api/v1/health/live",
            headers={
                "Origin": "https://approved.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        denied = client.options(
            "/api/v1/health/live",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://approved.example"
    assert "access-control-allow-origin" not in denied.headers
