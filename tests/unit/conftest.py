"""Unit-test-only environment fixtures."""

from __future__ import annotations

import os

import pytest

_TEST_MUTATION_TOKEN = "unit-test-mutation-token-value-0000001"
os.environ.setdefault("ASTRAFORGE_MUTATION_API_TOKEN", _TEST_MUTATION_TOKEN)


@pytest.fixture(autouse=True)
def protected_demo_test_environment(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provide inert protected configuration only inside the unit-test process."""

    nodeid = request.node.nodeid
    if "test_config.py" in nodeid:
        monkeypatch.delenv("ASTRAFORGE_MUTATION_API_TOKEN", raising=False)
        return

    monkeypatch.setenv("ASTRAFORGE_MUTATION_API_TOKEN", _TEST_MUTATION_TOKEN)
    if "test_phase5_execution_commands.py" not in nodeid:
        return
    monkeypatch.setenv("ASTRAFORGE_BINANCE_DEMO_BASE_URL", "https://demo-fapi.binance.com")
    monkeypatch.setenv("ASTRAFORGE_BINANCE_DEMO_API_KEY", "phase5-test-key")
    monkeypatch.setenv("ASTRAFORGE_BINANCE_DEMO_API_SECRET", "phase5-test-secret")
