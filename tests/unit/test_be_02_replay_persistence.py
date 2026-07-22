"""BE-02 regression tests for durable idempotency and replay protection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.core.security import MutationReplayGuard, ReplayClaimResult
from app.persistence.database import Persistence
from app.persistence.repositories import TradingStateRepositories

NOW = datetime(2026, 7, 19, 13, 3, 50, tzinfo=UTC)


def _repositories(database_url: str) -> TradingStateRepositories:
    persistence = Persistence(database_url)
    persistence.initialize()
    return TradingStateRepositories(persistence)


def test_replay_claim_survives_process_restart(tmp_path: Any) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'be02-restart.db'}"

    first = _repositories(database_url)
    first_guard = MutationReplayGuard(
        ttl_seconds=900,
        cache_limit=5000,
        repositories=first,
    )
    accepted = asyncio.run(
        first_guard.claim(
            key_hash="a" * 64,
            fingerprint="b" * 64,
            action="POST /api/v1/scanner/stop",
            now=NOW,
        )
    )
    first.persistence.close()

    restarted = _repositories(database_url)
    restarted_guard = MutationReplayGuard(
        ttl_seconds=900,
        cache_limit=5000,
        repositories=restarted,
    )
    replay = asyncio.run(
        restarted_guard.claim(
            key_hash="a" * 64,
            fingerprint="b" * 64,
            action="POST /api/v1/scanner/stop",
            now=NOW,
        )
    )
    restarted.persistence.close()

    assert accepted is ReplayClaimResult.ACCEPTED
    assert replay is ReplayClaimResult.REPLAY


def test_shared_database_coordinates_separate_application_instances(tmp_path: Any) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'be02-multi-instance.db'}"
    first = _repositories(database_url)
    second = _repositories(database_url)

    first_guard = MutationReplayGuard(
        ttl_seconds=900,
        cache_limit=5000,
        repositories=first,
    )
    second_guard = MutationReplayGuard(
        ttl_seconds=900,
        cache_limit=5000,
        repositories=second,
    )

    accepted = asyncio.run(
        first_guard.claim(
            key_hash="c" * 64,
            fingerprint="d" * 64,
            action="POST /api/v1/execution/demo/activate/signal-1",
            now=NOW,
        )
    )
    duplicate = asyncio.run(
        second_guard.claim(
            key_hash="c" * 64,
            fingerprint="d" * 64,
            action="POST /api/v1/execution/demo/activate/signal-1",
            now=NOW,
        )
    )
    conflicting_reuse = asyncio.run(
        second_guard.claim(
            key_hash="c" * 64,
            fingerprint="e" * 64,
            action="POST /api/v1/scanner/run-now",
            now=NOW,
        )
    )

    first.persistence.close()
    second.persistence.close()

    assert accepted is ReplayClaimResult.ACCEPTED
    assert duplicate is ReplayClaimResult.REPLAY
    assert conflicting_reuse is ReplayClaimResult.REUSED_FOR_DIFFERENT_REQUEST
