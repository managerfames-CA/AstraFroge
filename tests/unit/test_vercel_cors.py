from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def _client() -> TestClient:
    settings = Settings(
        environment="test",
        scanner_auto_start=False,
        mutation_auth_required=False,
        cors_origins=["http://localhost:5173"],
    )
    return TestClient(create_app(settings))


def test_astraforge_vercel_production_origin_is_allowed() -> None:
    with _client() as client:
        response = client.options(
            "/api/v1/health/live",
            headers={
                "Origin": "https://astra-forge-crypto-frontend.vercel.app",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == (
        "https://astra-forge-crypto-frontend.vercel.app"
    )


def test_astraforge_vercel_preview_origin_is_allowed() -> None:
    origin = "https://astra-forge-crypto-frontend-git-main-example.vercel.app"
    with _client() as client:
        response = client.options(
            "/api/v1/health/live",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


def test_unrelated_vercel_origin_is_rejected() -> None:
    with _client() as client:
        response = client.options(
            "/api/v1/health/live",
            headers={
                "Origin": "https://unrelated-project.vercel.app",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
