"""Shared API dependency factories."""

from functools import lru_cache

from app.core.config import get_settings
from app.integrations.binance.pooled_clients import (
    PooledBinanceDemoRecoveryClient,
    PooledBinancePublicClient,
)
from app.integrations.binance.public_client import BinancePublicClient
from app.persistence.execution_command_repository import ExecutionCommandRepository
from app.persistence.repositories import TradingStateRepositories
from app.persistence.service_adapters import PersistentExecutionService, PersistentRiskService
from app.scanner.constants import SCANNER_MAX_SYMBOLS, SCANNER_PREFILTER_POOL_SYMBOLS
from app.services.account_snapshot import (
    AccountSnapshotService,
    FreshAccountExecutionService,
    SnapshotAwarePrivateClient,
)
from app.services.decision_signals import (
    DecisionBackedSignalService,
    PersistentDecisionSignalService,
)
from app.services.execution import DemoExecutionService
from app.services.execution_command import ExecutionCommandService
from app.services.execution_facade import WorkerIsolatedExecutionService
from app.services.execution_leader_safety import (
    LeaderValidatedExecutionService,
    ValidatedExecutionLeaderLease,
)
from app.services.execution_private_adapter import (
    QueryBeforeRetrySnapshotPrivateClient,
)
from app.services.execution_worker import DemoExecutionWorker
from app.services.indicators import IndicatorService
from app.services.journal_performance import JournalPerformanceService
from app.services.market_data import MarketDataService
from app.services.recovery import AutomationRecoveryGate, StartupRecoveryCoordinator
from app.services.risk import RiskService
from app.services.scanner_runtime import ScannerSchedulerLease
from app.services.scanner_strategy_separated import (
    StrategySeparatedScannerService as ScannerService,
)
from app.services.scanner_universe import DirectionalScannerUniverse

__all__ = ["ScannerService"]
from app.services.shared_snapshots import (
    SharedClosedCandleMarketDataService,
    SharedIndicatorService,
)
from app.services.signal_decision import SignalDecisionEngine
from app.services.signals import SignalService
from app.services.trade_management import TradeManagementService
from app.services.universe import UniverseService

_runtime_repositories: TradingStateRepositories | None = None
_runtime_recovery_gate = AutomationRecoveryGate()
_runtime_leader_lease = ValidatedExecutionLeaderLease(None)
_runtime_scanner_lease = ScannerSchedulerLease(None)


def configure_runtime_repositories(repositories: TradingStateRepositories | None) -> None:
    """Set app-scoped persistence/recovery boundaries and rebuild dependent services."""

    global _runtime_repositories, _runtime_recovery_gate
    global _runtime_leader_lease, _runtime_scanner_lease
    _runtime_leader_lease.release()
    _runtime_scanner_lease.release()
    _runtime_repositories = repositories
    _runtime_recovery_gate = AutomationRecoveryGate()
    _runtime_leader_lease = ValidatedExecutionLeaderLease(
        repositories.persistence if repositories is not None else None
    )
    _runtime_scanner_lease = ScannerSchedulerLease(
        repositories.persistence if repositories is not None else None
    )
    get_market_service.cache_clear()
    get_universe_service.cache_clear()
    get_scanner_universe_service.cache_clear()
    get_indicator_service.cache_clear()
    get_scanner_service.cache_clear()
    get_signal_decision_engine.cache_clear()
    get_signal_service.cache_clear()
    get_account_snapshot_service.cache_clear()
    get_snapshot_private_client.cache_clear()
    get_risk_service.cache_clear()
    get_execution_command_repository.cache_clear()
    get_execution_command_service.cache_clear()
    get_execution_order_backend.cache_clear()
    get_execution_service.cache_clear()
    get_execution_worker.cache_clear()
    get_trade_management_service.cache_clear()
    get_journal_performance_service.cache_clear()
    get_startup_recovery_coordinator.cache_clear()


def get_recovery_gate() -> AutomationRecoveryGate:
    """Return the one authoritative automation-readiness gate for this app process."""

    return _runtime_recovery_gate


def get_execution_leader_lease() -> ValidatedExecutionLeaderLease:
    """Return the account-scoped, continuously validated PostgreSQL execution leader lease."""

    return _runtime_leader_lease


@lru_cache
def get_public_market_client() -> BinancePublicClient:
    """Build one process-scoped pooled Binance public client."""

    settings = get_settings()
    return PooledBinancePublicClient(
        base_url=settings.binance_public_base_url,
        timeout_seconds=settings.market_request_timeout_seconds,
        retry_attempts=settings.market_retry_attempts,
        retry_base_delay_seconds=settings.market_retry_base_delay_seconds,
        rate_limit_max_delay_seconds=settings.market_rate_limit_max_delay_seconds,
    )


@lru_cache
def get_private_demo_client() -> PooledBinanceDemoRecoveryClient | None:
    """Build one process-scoped pooled Demo client with Phase 1 recovery methods."""

    settings = get_settings()
    if not settings.demo_credentials_configured or settings.binance_demo_base_url is None:
        return None
    assert settings.binance_demo_api_key is not None
    assert settings.binance_demo_api_secret is not None
    return PooledBinanceDemoRecoveryClient(
        base_url=settings.binance_demo_base_url,
        api_key=settings.binance_demo_api_key.get_secret_value(),
        api_secret=settings.binance_demo_api_secret.get_secret_value(),
        timeout_seconds=settings.market_request_timeout_seconds,
        recv_window_ms=settings.binance_demo_recv_window_ms,
    )


@lru_cache
def get_account_snapshot_service() -> AccountSnapshotService | None:
    """Return the shared bounded-freshness Demo account snapshot authority."""

    client = get_private_demo_client()
    if client is None:
        return None
    return AccountSnapshotService(client, freshness_seconds=2.0)


@lru_cache
def get_snapshot_private_client() -> SnapshotAwarePrivateClient | None:
    """Reuse account state and make protective mutations query-before-retry."""

    client = get_private_demo_client()
    snapshots = get_account_snapshot_service()
    if client is None or snapshots is None:
        return None
    return QueryBeforeRetrySnapshotPrivateClient(client, snapshots)


@lru_cache
def get_market_service() -> MarketDataService:
    """Build one process-scoped shared closed-candle Market Data authority."""

    settings = get_settings()
    return SharedClosedCandleMarketDataService(
        get_public_market_client(),
        cache_ttl_seconds=settings.market_cache_ttl_seconds,
        stale_ttl_seconds=settings.market_stale_ttl_seconds,
    )


@lru_cache
def get_universe_service() -> UniverseService:
    """Build one process-scoped public Universe service."""

    settings = get_settings()
    return UniverseService(
        get_public_market_client(),
        max_symbols=settings.universe_max_symbols,
        min_quote_volume=settings.universe_min_quote_volume,
        max_spread_bps=settings.universe_max_spread_bps,
    )


@lru_cache
def get_scanner_universe_service() -> DirectionalScannerUniverse:
    """Build the Scanner broad pool and 1H directional final universe prefilter."""

    settings = get_settings()
    broad_source = UniverseService(
        get_public_market_client(),
        max_symbols=SCANNER_PREFILTER_POOL_SYMBOLS,
        min_quote_volume=settings.universe_min_quote_volume,
        max_spread_bps=settings.universe_max_spread_bps,
    )
    return DirectionalScannerUniverse(
        broad_source,
        get_market_service(),
        max_symbols=SCANNER_MAX_SYMBOLS,
    )


@lru_cache
def get_indicator_service() -> IndicatorService:
    """Build one process-scoped indicator snapshot cache over shared closed candles."""

    return SharedIndicatorService(get_market_service())


@lru_cache
def get_scanner_service() -> ScannerService:
    """Build fact-only Scanner discovery with deterministic Strategy evaluation."""

    return ScannerService(
        get_market_service(),
        get_scanner_universe_service(),
        get_indicator_service(),
        lease=_runtime_scanner_lease,
        settings=get_settings(),
    )


@lru_cache
def get_signal_decision_engine() -> SignalDecisionEngine:
    """Return the single process-scoped final Signal eligibility authority."""

    return SignalDecisionEngine()


@lru_cache
def get_signal_service() -> SignalService:
    """Build decision-backed Signals with durable restart-safe deduplication."""

    scanner = get_scanner_service()
    decisions = get_signal_decision_engine()
    if _runtime_repositories is None:
        return DecisionBackedSignalService(scanner, decisions)
    return PersistentDecisionSignalService(
        scanner,
        decisions,
        _runtime_repositories,
    )


@lru_cache
def get_risk_service() -> RiskService:
    """Build Risk over READY-only Signals and the shared account snapshot client."""

    signal_service = get_signal_service()
    settings = get_settings()
    private_client = get_snapshot_private_client()
    if _runtime_repositories is None:
        return RiskService(signal_service, settings, private_client)
    return PersistentRiskService(
        signal_service,
        settings,
        private_client,
        _runtime_repositories,
    )


@lru_cache
def get_execution_command_repository() -> ExecutionCommandRepository | None:
    """Return the durable command queue; automation never falls back to memory."""

    if _runtime_repositories is None:
        return None
    return ExecutionCommandRepository(_runtime_repositories.persistence)


@lru_cache
def get_execution_command_service() -> ExecutionCommandService:
    """Build the only READY/Risk-to-durable-command boundary."""

    return ExecutionCommandService(
        get_signal_service(),
        get_risk_service(),
        get_settings(),
        get_account_snapshot_service(),
        get_execution_command_repository(),
    )


@lru_cache
def get_execution_order_backend() -> DemoExecutionService:
    """Build the verified order backend; only DemoExecutionWorker receives this object."""

    risk_service = get_risk_service()
    settings = get_settings()
    private_client = get_snapshot_private_client()
    if _runtime_repositories is None:
        return DemoExecutionService(risk_service, settings, private_client)
    return PersistentExecutionService(
        risk_service,
        settings,
        private_client,
        _runtime_repositories,
    )


@lru_cache
def get_execution_service() -> DemoExecutionService:
    """Build a read-compatible facade that blocks every direct new-entry call."""

    inner: DemoExecutionService = get_execution_order_backend()
    settings = get_settings()
    snapshots = get_account_snapshot_service()
    if snapshots is not None:
        inner = FreshAccountExecutionService(
            inner,
            snapshots,
            refresh_required=settings.execution_enabled,
        )
    guarded: DemoExecutionService = LeaderValidatedExecutionService(
        inner,
        get_recovery_gate(),
        get_execution_leader_lease(),
        recovery_required=settings.execution_enabled,
    )
    return WorkerIsolatedExecutionService(
        guarded,
        get_execution_command_service(),
    )


@lru_cache
def get_execution_worker() -> DemoExecutionWorker:
    """Return the single process-scoped owner of new Binance Demo entry writes."""

    return DemoExecutionWorker(
        get_execution_command_service(),
        get_execution_order_backend(),
        get_settings(),
        get_recovery_gate(),
        get_execution_leader_lease(),
    )


@lru_cache
def get_trade_management_service() -> TradeManagementService:
    """Build Trade Management over shared private snapshot/delegation client."""

    return TradeManagementService(get_execution_service(), get_snapshot_private_client())


@lru_cache
def get_journal_performance_service() -> JournalPerformanceService:
    """Build one process-scoped truthful Journal/Performance Engine."""

    return JournalPerformanceService(get_trade_management_service())


@lru_cache
def get_startup_recovery_coordinator() -> StartupRecoveryCoordinator:
    """Build ordered Phase 1 recovery using the raw pooled recovery client."""

    persistence = _runtime_repositories.persistence if _runtime_repositories is not None else None
    return StartupRecoveryCoordinator(
        settings=get_settings(),
        gate=get_recovery_gate(),
        leader_lease=get_execution_leader_lease(),
        scanner_service=get_scanner_service(),
        signal_service=get_signal_service(),
        execution_service=get_execution_service(),
        private_client=get_private_demo_client(),
        persistence=persistence,
    )


async def close_runtime_clients() -> None:
    """Close pooled HTTP clients cleanly and prevent reuse of closed process resources."""

    public_client = get_public_market_client()
    close_public = getattr(public_client, "aclose", None)
    if callable(close_public):
        await close_public()
    private_client = get_private_demo_client()
    if private_client is not None:
        private_client.close()
    get_public_market_client.cache_clear()
    get_private_demo_client.cache_clear()
