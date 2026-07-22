"""Database lifecycle and transaction boundary for durable trading state."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings


class PersistenceConfigurationError(RuntimeError):
    """Persistence is missing or unsafe for the selected environment."""


def _configured_database_url(database_url: str | None = None) -> str | None:
    if database_url is not None:
        return database_url
    explicit = os.getenv("ASTRAFORGE_DATABASE_URL")
    if explicit is not None and explicit.strip():
        return explicit
    return os.getenv("DATABASE_URL")


def _normalize_postgresql_driver(database_url: str) -> str:
    """Use the installed psycopg v3 driver for generic PostgreSQL URLs."""

    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    return database_url


def validate_database_url(settings: Settings, database_url: str | None = None) -> str | None:
    """Return a validated URL; staging/production never fall back to memory."""

    raw = _configured_database_url(database_url)
    if raw is None or not raw.strip():
        if settings.environment in {"staging", "production"}:
            raise PersistenceConfigurationError(
                "ASTRAFORGE_DATABASE_URL or DATABASE_URL is required in staging and production"
            )
        return None
    normalized = _normalize_postgresql_driver(raw.strip())
    url = make_url(normalized)
    if url.get_backend_name() == "sqlite":
        database = url.database or ""
        is_memory = database in {"", ":memory:"} or "mode=memory" in str(url)
        if settings.environment in {"staging", "production"}:
            raise PersistenceConfigurationError(
                "SQLite is not an approved staging/production persistence backend"
            )
        if is_memory and settings.environment != "test":
            raise PersistenceConfigurationError(
                "In-memory SQLite is allowed only for isolated tests"
            )
    elif url.get_backend_name() != "postgresql":
        raise PersistenceConfigurationError("Persistence backend must be PostgreSQL")
    return normalized


class Persistence:
    """Central engine/session owner with explicit atomic transactions."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(
            database_url,
            future=True,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> Persistence | None:
        database_url = validate_database_url(settings)
        return cls(database_url) if database_url is not None else None

    def initialize(self) -> None:
        """Upgrade to the latest schema and prove the database is reachable."""

        migration_config = Config("alembic.ini")
        migration_config.set_main_option(
            "sqlalchemy.url", self.database_url.replace("%", "%%")
        )
        command.upgrade(migration_config, "head")
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """Commit all related records atomically or roll everything back."""

        session = self._sessions()
        try:
            with session.begin():
                yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def close(self) -> None:
        self.engine.dispose()
