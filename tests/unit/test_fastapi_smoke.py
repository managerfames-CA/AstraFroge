"""Smoke test for FastAPI application import and schema generation."""

from __future__ import annotations

import sys


def run_smoke_test() -> int:
    """Import the FastAPI application, trigger schema generation, and list routes."""
    print("=== Starting FastAPI Import Smoke Verification ===")
    try:
        from app.main import app
    except Exception as exc:
        print(f"ERROR: Failed to import FastAPI app object: {exc}", file=sys.stderr)
        return 1

    try:
        print("FastAPI app imported successfully. Generating OpenAPI schema...")
        openapi_schema = app.openapi()
        if not openapi_schema:
            print("ERROR: OpenAPI schema is empty or None.", file=sys.stderr)
            return 1
        print("OpenAPI schema generated successfully.")
    except Exception as exc:
        print(f"ERROR: Failed to generate OpenAPI schema: {exc}", file=sys.stderr)
        return 1

    print("\n--- Registered Endpoints / Routers Verification ---")
    routes_found = 0
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        name = getattr(route, "name", None)
        methods_str = ",".join(methods) if methods else "N/A"
        print(f"Route: [{methods_str}] {path} -> {name}")
        routes_found += 1

    print(f"\nTotal routes verified: {routes_found}")
    if routes_found == 0:
        print("ERROR: No routes found in the FastAPI application.", file=sys.stderr)
        return 1

    print("=== FastAPI Import Smoke Verification Passed ===")
    return 0


def test_fastapi_app_import_and_routing() -> None:
    """Unit test wrapper for pytest to run during the standard test suite execution."""
    assert run_smoke_test() == 0


if __name__ == "__main__":
    sys.exit(run_smoke_test())
