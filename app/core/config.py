"""Typed application settings."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BINANCE_USDM_DEMO_HOST = "demo-fapi.binance.com"


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="ASTRAFORGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AstraForge Crypto Backend"
    app_version: str = "0.4.0"
    environment: Literal["development", "test", "staging", "production"] = "development"
    api_prefix: str = "/api/v1"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    cors_allow_credentials: bool = False
    mutation_auth_required: bool = True
    mutation_api_token: SecretStr | None = None
    mutation_replay_ttl_seconds: int = Field(default=900, ge=60, le=86400)
    mutation_replay_cache_limit: int = Field(default=5000, ge=100, le=100000)
    execution_enabled: bool = False
    execution_take_profit_r_multiple: Decimal = Field(default=Decimal("0"), ge=0, le=20)
    binance_public_base_url: str = "https://fapi.binance.com"
    binance_demo_base_url: str | None = None
    market_request_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    market_retry_attempts: int = Field(default=3, ge=1, le=5)
    market_retry_base_delay_seconds: float = Field(default=0.25, ge=0, le=2)
    market_rate_limit_max_delay_seconds: float = Field(default=60.0, gt=0, le=300)
    market_cache_ttl_seconds: float = Field(default=2.0, ge=0, le=60)
    market_stale_ttl_seconds: float = Field(default=30.0, ge=0, le=300)
    universe_max_symbols: int = Field(default=50, ge=1, le=200)
    universe_min_quote_volume: Decimal = Field(default=Decimal("10000000"), ge=0)
    universe_max_spread_bps: Decimal = Field(default=Decimal("10"), gt=0, le=1000)
    risk_per_trade_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    risk_daily_loss_limit_percent: Decimal = Field(default=Decimal("3"), ge=0, le=100)
    risk_daily_profit_lock_percent: Decimal = Field(default=Decimal("5"), ge=0, le=100)
    risk_max_open_trades: int = Field(default=4, ge=1, le=50)
    risk_max_margin_exposure_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    scanner_auto_start: bool = False
    binance_demo_recv_window_ms: int = Field(default=5000, ge=1000, le=60000)
    binance_demo_api_key: SecretStr | None = None
    binance_demo_api_secret: SecretStr | None = None

    @field_validator("scanner_auto_start", mode="before")
    @classmethod
    def validate_scanner_auto_start(cls, value: Any) -> bool:
        """Enforce strict boolean validation for scanner auto start."""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            val_lower = value.strip().lower()
            if val_lower in {"true", "1"}:
                return True
            if val_lower in {"false", "0"}:
                return False
            raise ValueError("Invalid boolean value for ASTRAFORGE_SCANNER_AUTO_START")
        if isinstance(value, (int, float)):
            if value == 1:
                return True
            if value == 0:
                return False
        raise ValueError("Invalid boolean value for ASTRAFORGE_SCANNER_AUTO_START")

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, origins: list[str]) -> list[str]:
        """Require an explicit HTTP(S) allowlist and reject wildcards."""

        normalized: list[str] = []
        for raw_origin in origins:
            origin = raw_origin.strip().rstrip("/")
            if origin == "*":
                raise ValueError("Wildcard CORS origins are not permitted")
            parsed = urlparse(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"Invalid CORS origin: {raw_origin}")
            normalized.append(origin)
        if not normalized:
            raise ValueError("At least one approved CORS origin is required")
        return list(dict.fromkeys(normalized))

    @field_validator("mutation_api_token")
    @classmethod
    def validate_mutation_api_token(cls, value: SecretStr | None) -> SecretStr | None:
        """Require a strong exact ASCII operator token or an empty lock state."""

        if value is None or not value.get_secret_value():
            return value
        secret = value.get_secret_value()
        if secret != secret.strip():
            raise ValueError("Mutation API token must not contain surrounding whitespace")
        if not secret.isascii():
            raise ValueError("Mutation API token must contain ASCII characters only")
        if len(secret) < 32:
            raise ValueError("Mutation API token must contain at least 32 characters")
        return value

    @field_validator("binance_public_base_url")
    @classmethod
    def validate_binance_public_base_url(cls, value: str) -> str:
        """Require a secure explicit Binance public API base URL."""

        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Binance public base URL must be HTTPS")
        return normalized

    @field_validator("binance_demo_base_url")
    @classmethod
    def validate_binance_demo_base_url(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        """Allow the official Demo endpoint and reserved fake hosts in isolated tests."""

        if value is None or not value.strip():
            return None
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Binance Demo base URL must be HTTPS")
        hostname = parsed.hostname or ""
        is_official = hostname == _BINANCE_USDM_DEMO_HOST
        is_reserved_test_fake = info.data.get("environment") == "test" and hostname.endswith(
            ".example"
        )
        if (
            not (is_official or is_reserved_test_fake)
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Binance Demo base URL must be the official USD-M Futures Demo endpoint"
            )
        return normalized

    @model_validator(mode="after")
    def validate_market_cache_window(self) -> Settings:
        """Require stale fallback to be at least as long as the fresh cache window."""

        if self.market_stale_ttl_seconds < self.market_cache_ttl_seconds:
            raise ValueError("Market stale TTL must be greater than or equal to cache TTL")
        return self

    @model_validator(mode="after")
    def validate_mutation_security_mode(self) -> Settings:
        """Prevent security bypass in staging and production."""

        if self.environment in {"staging", "production"} and not self.mutation_auth_required:
            raise ValueError("Mutation authentication cannot be disabled outside development/test")
        return self

    @model_validator(mode="after")
    def default_scanner_auto_start_for_tests(self) -> Settings:
        """Keep scanner auto-start off in tests unless it is explicitly enabled."""

        if self.environment == "test" and "scanner_auto_start" not in self.model_fields_set:
            self.scanner_auto_start = False
        return self

    @model_validator(mode="after")
    def validate_demo_credentials(self) -> Settings:
        """Reject half-configured Demo credentials while allowing both to be absent."""

        key_present = bool(
            self.binance_demo_api_key and self.binance_demo_api_key.get_secret_value()
        )
        secret_present = bool(
            self.binance_demo_api_secret and self.binance_demo_api_secret.get_secret_value()
        )
        if key_present != secret_present:
            raise ValueError("Binance Demo API key and secret must be configured together")
        return self

    @model_validator(mode="after")
    def validate_demo_execution_unlock(self) -> Settings:
        """Unlock execution only with protected mutations and an approved Demo endpoint."""

        if not self.execution_enabled:
            return self
        if not self.mutation_auth_required:
            raise ValueError("Demo execution requires mutation authentication")
        if not self.mutation_token_configured:
            raise ValueError("Demo execution requires a configured mutation API token")
        if not self.demo_credentials_configured:
            raise ValueError("Demo execution requires configured Binance Demo credentials")
        if self.binance_demo_base_url is None:
            raise ValueError("Demo execution requires the official Binance Demo base URL")
        return self

    @property
    def demo_credentials_configured(self) -> bool:
        """Return whether both Demo credential fields have non-empty values."""

        return bool(
            self.binance_demo_api_key
            and self.binance_demo_api_key.get_secret_value()
            and self.binance_demo_api_secret
            and self.binance_demo_api_secret.get_secret_value()
        )

    @property
    def mutation_token_configured(self) -> bool:
        """Return whether a non-empty mutation API token is configured."""

        return bool(self.mutation_api_token and self.mutation_api_token.get_secret_value())

    @property
    def known_secret_values(self) -> tuple[str, ...]:
        """Return configured secret values for log redaction without exposing them."""

        values: list[str] = []
        for secret in (
            self.mutation_api_token,
            self.binance_demo_api_key,
            self.binance_demo_api_secret,
        ):
            if secret is not None and secret.get_secret_value():
                values.append(secret.get_secret_value())
        return tuple(values)


@lru_cache
def get_settings() -> Settings:
    """Return a cached settings instance for the process."""

    return Settings()
