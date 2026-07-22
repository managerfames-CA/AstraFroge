"""Fail-closed authorization, replay protection, and audit context for mutations."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.persistence.repositories import TradingStateRepositories

_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
_BEARER = HTTPBearer(auto_error=False)
MUTATION_OPENAPI_PATHS = (
    "/scanner/start",
    "/scanner/stop",
    "/scanner/run-now",
    "/execution/demo/activate/{signal_id}",
    "/trade-management/close/{trade_id}",
)


class ReplayClaimResult(StrEnum):
    """Result of claiming a single-use idempotency key."""

    ACCEPTED = "ACCEPTED"
    REPLAY = "REPLAY"
    REUSED_FOR_DIFFERENT_REQUEST = "REUSED_FOR_DIFFERENT_REQUEST"
    CAPACITY_EXHAUSTED = "CAPACITY_EXHAUSTED"


@dataclass(frozen=True)
class MutationAuthorization:
    """Authorized mutation metadata attached to the current request."""

    request_id: str
    actor: str
    action: str
    idempotency_key_hash: str
    request_fingerprint: str
    authorized_at: datetime


@dataclass(frozen=True)
class _ReplayEntry:
    fingerprint: str
    expires_at: datetime


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class MutationReplayGuard:
    """Bounded process-scoped registry of single-use mutation idempotency keys."""

    def __init__(
        self,
        *,
        ttl_seconds: int,
        cache_limit: int,
        repositories: TradingStateRepositories | None = None,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("Replay TTL must be positive")
        if cache_limit < 1:
            raise ValueError("Replay cache limit must be positive")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._cache_limit = cache_limit
        self._repositories = repositories
        self._entries: OrderedDict[str, _ReplayEntry] = OrderedDict()
        self._lock = asyncio.Lock()

    async def claim(
        self,
        *,
        key_hash: str,
        fingerprint: str,
        action: str,
        now: datetime | None = None,
    ) -> ReplayClaimResult:
        """Atomically reserve one idempotency key and reject duplicates."""

        current = (now or datetime.now(UTC)).astimezone(UTC)
        if self._repositories is not None:
            return self._claim_durable(
                key_hash=key_hash,
                fingerprint=fingerprint,
                action=action,
                current=current,
            )
        async with self._lock:
            self._prune(current)
            existing = self._entries.get(key_hash)
            if existing is not None:
                if hmac.compare_digest(existing.fingerprint, fingerprint):
                    return ReplayClaimResult.REPLAY
                return ReplayClaimResult.REUSED_FOR_DIFFERENT_REQUEST
            if len(self._entries) >= self._cache_limit:
                return ReplayClaimResult.CAPACITY_EXHAUSTED

            self._entries[key_hash] = _ReplayEntry(
                fingerprint=fingerprint,
                expires_at=current + self._ttl,
            )
            return ReplayClaimResult.ACCEPTED

    def _claim_durable(
        self,
        *,
        key_hash: str,
        fingerprint: str,
        action: str,
        current: datetime,
    ) -> ReplayClaimResult:
        assert self._repositories is not None
        accepted, existing = self._repositories.claim_mutation_replay(
            key_hash=key_hash,
            fingerprint=fingerprint,
            action=action,
            now=current,
            expires_at=current + self._ttl,
            cache_limit=self._cache_limit,
        )
        if accepted:
            return ReplayClaimResult.ACCEPTED
        if existing is None:
            return ReplayClaimResult.CAPACITY_EXHAUSTED
        if _as_utc(existing.expires_at) <= current:
            return ReplayClaimResult.ACCEPTED
        if hmac.compare_digest(existing.fingerprint, fingerprint):
            return ReplayClaimResult.REPLAY
        return ReplayClaimResult.REUSED_FOR_DIFFERENT_REQUEST

    def _prune(self, now: datetime) -> None:
        expired = [
            key for key, entry in self._entries.items() if entry.expires_at <= now
        ]
        for key in expired:
            self._entries.pop(key, None)


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) and value else "unavailable"


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or None
    return request.client.host if request.client is not None else None


def _fingerprint(request: Request, body: bytes) -> str:
    payload = b"\0".join(
        (
            request.method.upper().encode(),
            request.url.path.encode(),
            request.url.query.encode(),
            body,
        )
    )
    return hashlib.sha256(payload).hexdigest()


def _set_audit_context(
    request: Request,
    *,
    actor: str,
    idempotency_key_hash: str | None,
) -> None:
    request.state.mutation_audit = {
        "audit_event": "mutation_request",
        "actor": actor,
        "action": f"{request.method.upper()} {request.url.path}",
        "resource": request.url.path,
        "idempotency_key_hash": idempotency_key_hash,
        "client_ip": _client_ip(request),
    }


def _valid_token(
    credentials: HTTPAuthorizationCredentials | None,
    configured_token: str,
) -> bool:
    if credentials is None or credentials.scheme.lower() != "bearer":
        return False
    try:
        supplied = credentials.credentials.encode("ascii")
        configured = configured_token.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(supplied, configured)


async def authorize_mutation(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_BEARER),
    ],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> MutationAuthorization:
    """Authorize one mutation and atomically reserve its idempotency key."""

    request_id = _request_id(request)
    action = f"{request.method.upper()} {request.url.path}"
    _set_audit_context(request, actor="unauthenticated", idempotency_key_hash=None)

    if not settings.mutation_auth_required:
        _set_audit_context(
            request,
            actor="security-bypass-test",
            idempotency_key_hash="disabled",
        )
        return MutationAuthorization(
            request_id=request_id,
            actor="security-bypass-test",
            action=action,
            idempotency_key_hash="disabled",
            request_fingerprint="disabled",
            authorized_at=datetime.now(UTC),
        )

    if not settings.mutation_token_configured:
        raise AppError(
            status_code=503,
            code="MUTATION_AUTH_NOT_CONFIGURED",
            message="Mutation endpoints are locked until operator authorization is configured",
        )

    configured = settings.mutation_api_token
    assert configured is not None
    if not _valid_token(credentials, configured.get_secret_value()):
        raise AppError(
            status_code=401,
            code="INVALID_MUTATION_CREDENTIALS",
            message="Valid operator authorization is required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if idempotency_key is None:
        raise AppError(
            status_code=400,
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="Idempotency-Key is required for mutation requests",
        )
    normalized_key = idempotency_key.strip()
    if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized_key):
        raise AppError(
            status_code=400,
            code="INVALID_IDEMPOTENCY_KEY",
            message="Idempotency-Key must be 16-128 safe ASCII characters",
        )

    body = await request.body()
    fingerprint = _fingerprint(request, body)
    key_hash = hashlib.sha256(normalized_key.encode()).hexdigest()
    _set_audit_context(request, actor="operator", idempotency_key_hash=key_hash)

    guard = getattr(request.app.state, "mutation_replay_guard", None)
    if not isinstance(guard, MutationReplayGuard):
        raise AppError(
            status_code=503,
            code="MUTATION_SECURITY_UNAVAILABLE",
            message="Mutation replay protection is unavailable",
        )
    result = await guard.claim(key_hash=key_hash, fingerprint=fingerprint, action=action)
    if result is ReplayClaimResult.REPLAY:
        raise AppError(
            status_code=409,
            code="REPLAY_DETECTED",
            message="This mutation request has already been submitted",
        )
    if result is ReplayClaimResult.REUSED_FOR_DIFFERENT_REQUEST:
        raise AppError(
            status_code=409,
            code="IDEMPOTENCY_KEY_REUSED",
            message="This Idempotency-Key was already used for another request",
        )
    if result is ReplayClaimResult.CAPACITY_EXHAUSTED:
        raise AppError(
            status_code=503,
            code="REPLAY_GUARD_CAPACITY_EXHAUSTED",
            message="Mutation replay protection is at capacity; request was not accepted",
        )

    return MutationAuthorization(
        request_id=request_id,
        actor="operator",
        action=action,
        idempotency_key_hash=key_hash,
        request_fingerprint=fingerprint,
        authorized_at=datetime.now(UTC),
    )
