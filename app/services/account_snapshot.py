"""Shared, versioned Binance Demo account snapshots for Phase 2."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from threading import Condition, RLock
from typing import Any, Protocol

from app.core.errors import AppError
from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.schemas.execution import (
    DemoAccountBalance,
    DemoExecutionAccountResponse,
    DemoExecutionActivateRequest,
    DemoExecutionPlanList,
    DemoExecutionStatusResponse,
    DemoPositionSnapshot,
    DemoTradeRecord,
    DemoTradeRecordList,
)
from app.schemas.scanner import ScannerDirection
from app.services.execution import DemoExecutionService

_TRADING_INCOME_TYPES = frozenset({"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"})


class AccountSnapshotPrivateClient(Protocol):
    """Private API surface needed to build one authoritative account snapshot."""

    def account(self) -> dict[str, Any]: ...

    def positions(self) -> list[dict[str, Any]]: ...

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]: ...


class SnapshotDelegatingPrivateClient(AccountSnapshotPrivateClient, Protocol):
    """Full private surface proxied without changing existing execution semantics."""

    def exchange_info(self) -> dict[str, Any]: ...

    def mark_price(self, symbol: str) -> dict[str, Any]: ...

    def position_mode(self) -> dict[str, Any]: ...

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]: ...

    def query_algo_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]: ...

    def cancel_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]: ...

    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str,
        new_client_order_id: str,
        reduce_only: bool = False,
    ) -> dict[str, Any]: ...

    def place_protective_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str,
        stop_price: str,
        new_client_order_id: str,
    ) -> dict[str, Any]: ...

    def open_orders(self) -> list[dict[str, Any]]: ...

    def open_algo_orders(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class AccountBalanceSnapshot:
    asset: str
    wallet_balance: Decimal
    available_balance: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class AccountPositionSnapshot:
    symbol: str
    position_amount: Decimal
    leverage: int
    entry_price: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class AccountIncomeSnapshot:
    income_type: str
    income: Decimal
    time_ms: int | None


@dataclass(frozen=True)
class AccountSnapshot:
    """Immutable normalized account state reused by downstream consumers."""

    snapshot_id: str
    captured_at: datetime
    source: str
    source_healthy: bool
    can_trade: bool
    total_wallet_balance_usdt: Decimal
    available_balance_usdt: Decimal
    total_unrealized_pnl_usdt: Decimal
    total_initial_margin_usdt: Decimal
    balances: tuple[AccountBalanceSnapshot, ...]
    positions: tuple[AccountPositionSnapshot, ...]
    income: tuple[AccountIncomeSnapshot, ...]


@dataclass(frozen=True)
class AccountSnapshotStatus:
    cache_hits: int
    refresh_count: int
    snapshot_age_seconds: float | None
    fresh: bool
    last_successful_refresh: datetime | None
    refresh_error: str | None
    snapshot_id: str | None


class AccountSnapshotPayloadError(RuntimeError):
    """The private API responded, but required account state could not be proven."""


class AccountSnapshotService:
    """Single-flight bounded-freshness authority for Demo account/position/income state."""

    def __init__(
        self,
        client: AccountSnapshotPrivateClient,
        *,
        freshness_seconds: float = 2.0,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if freshness_seconds < 0:
            raise ValueError("Account snapshot freshness must be non-negative")
        self._client = client
        self._freshness_seconds = freshness_seconds
        self._now = now_provider or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._snapshot: AccountSnapshot | None = None
        self._refreshing = False
        self._cache_hits = 0
        self._refresh_count = 0
        self._last_successful_refresh: datetime | None = None
        self._refresh_error: str | None = None

    def _age_seconds(self, snapshot: AccountSnapshot) -> float:
        return max(0.0, (self._now() - snapshot.captured_at).total_seconds())

    def status(self) -> AccountSnapshotStatus:
        with self._lock:
            snapshot = self._snapshot
            age = self._age_seconds(snapshot) if snapshot is not None else None
            return AccountSnapshotStatus(
                cache_hits=self._cache_hits,
                refresh_count=self._refresh_count,
                snapshot_age_seconds=age,
                fresh=bool(age is not None and age <= self._freshness_seconds),
                last_successful_refresh=self._last_successful_refresh,
                refresh_error=self._refresh_error,
                snapshot_id=snapshot.snapshot_id if snapshot is not None else None,
            )

    def invalidate(self) -> None:
        """Discard state after an account-changing side effect; never mutate an old snapshot."""

        with self._condition:
            self._snapshot = None
            self._condition.notify_all()

    def get(self, *, require_fresh: bool = False) -> AccountSnapshot:
        """Return bounded-fresh state; concurrent refreshers share one private poll set."""

        with self._condition:
            observed_id = self._snapshot.snapshot_id if self._snapshot is not None else None
            if (
                not require_fresh
                and self._snapshot is not None
                and self._age_seconds(self._snapshot) <= self._freshness_seconds
            ):
                self._cache_hits += 1
                return self._snapshot

            while self._refreshing:
                self._condition.wait()
                if self._snapshot is not None:
                    if require_fresh and self._snapshot.snapshot_id != observed_id:
                        self._cache_hits += 1
                        return self._snapshot
                    if (
                        not require_fresh
                        and self._age_seconds(self._snapshot) <= self._freshness_seconds
                    ):
                        self._cache_hits += 1
                        return self._snapshot
            self._refreshing = True

        try:
            snapshot = self._refresh()
        except Exception as exc:
            with self._condition:
                self._refresh_error = type(exc).__name__
                self._refreshing = False
                self._condition.notify_all()
            raise

        with self._condition:
            self._snapshot = snapshot
            self._refresh_count += 1
            self._last_successful_refresh = snapshot.captured_at
            self._refresh_error = None
            self._refreshing = False
            self._condition.notify_all()
            return snapshot

    def force_refresh(self) -> AccountSnapshot:
        """Execution-sensitive path: prove a newly fetched snapshot or fail closed."""

        return self.get(require_fresh=True)

    def _refresh(self) -> AccountSnapshot:
        now = self._now().astimezone(UTC)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        account = self._client.account()
        positions = self._client.positions()
        income = self._client.income_history(
            start_time_ms=int(start_of_day.timestamp() * 1000),
            end_time_ms=int(now.timestamp() * 1000),
            limit=1000,
        )
        if len(income) >= 1000:
            raise AccountSnapshotPayloadError("Income snapshot exceeded bounded result size")

        can_trade = account.get("canTrade")
        if not isinstance(can_trade, bool):
            raise AccountSnapshotPayloadError("Account canTrade is invalid")
        wallet = self._decimal(account, "totalWalletBalance", nonnegative=True)
        available = self._decimal(account, "availableBalance", nonnegative=True)
        unrealized = self._decimal(account, "totalUnrealizedProfit")
        initial_margin = self._decimal(account, "totalInitialMargin", nonnegative=True)

        balances: list[AccountBalanceSnapshot] = []
        raw_assets = account.get("assets", [])
        if not isinstance(raw_assets, list):
            raise AccountSnapshotPayloadError("Account assets payload is invalid")
        for item in raw_assets:
            if not isinstance(item, dict):
                raise AccountSnapshotPayloadError("Account asset payload is invalid")
            asset = item.get("asset")
            if not isinstance(asset, str) or not asset:
                raise AccountSnapshotPayloadError("Account asset identity is invalid")
            balances.append(
                AccountBalanceSnapshot(
                    asset=asset,
                    wallet_balance=self._decimal(item, "walletBalance"),
                    available_balance=self._decimal(item, "availableBalance"),
                    unrealized_pnl=self._decimal(item, "unrealizedProfit"),
                )
            )

        normalized_positions: list[AccountPositionSnapshot] = []
        for item in positions:
            symbol = item.get("symbol")
            if not isinstance(symbol, str) or not symbol:
                raise AccountSnapshotPayloadError("Position symbol is invalid")
            leverage_decimal = self._decimal(item, "leverage")
            if leverage_decimal < 1 or leverage_decimal != leverage_decimal.to_integral_value():
                raise AccountSnapshotPayloadError("Position leverage is invalid")
            normalized_positions.append(
                AccountPositionSnapshot(
                    symbol=symbol,
                    position_amount=self._decimal(item, "positionAmt"),
                    leverage=int(leverage_decimal),
                    entry_price=self._decimal(item, "entryPrice", default="0"),
                    unrealized_pnl=self._decimal(item, "unRealizedProfit", default="0"),
                )
            )

        normalized_income: list[AccountIncomeSnapshot] = []
        for item in income:
            income_type = item.get("incomeType")
            if not isinstance(income_type, str):
                raise AccountSnapshotPayloadError("Income type is invalid")
            if income_type not in _TRADING_INCOME_TYPES:
                continue
            raw_time = item.get("time")
            time_ms = int(raw_time) if raw_time is not None else None
            normalized_income.append(
                AccountIncomeSnapshot(
                    income_type=income_type,
                    income=self._decimal(item, "income"),
                    time_ms=time_ms,
                )
            )

        identity_payload = {
            "captured_at": now.isoformat(),
            "can_trade": can_trade,
            "wallet": format(wallet, "f"),
            "available": format(available, "f"),
            "unrealized": format(unrealized, "f"),
            "initial_margin": format(initial_margin, "f"),
            "positions": [
                (
                    item.symbol,
                    format(item.position_amount, "f"),
                    item.leverage,
                    format(item.entry_price, "f"),
                    format(item.unrealized_pnl, "f"),
                )
                for item in normalized_positions
            ],
            "income": [
                (item.income_type, format(item.income, "f"), item.time_ms)
                for item in normalized_income
            ],
        }
        snapshot_id = hashlib.sha256(
            json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return AccountSnapshot(
            snapshot_id=snapshot_id,
            captured_at=now,
            source="binance_usdm_demo_private",
            source_healthy=True,
            can_trade=can_trade,
            total_wallet_balance_usdt=wallet,
            available_balance_usdt=available,
            total_unrealized_pnl_usdt=unrealized,
            total_initial_margin_usdt=initial_margin,
            balances=tuple(balances),
            positions=tuple(normalized_positions),
            income=tuple(normalized_income),
        )

    @staticmethod
    def _decimal(
        payload: dict[str, Any],
        key: str,
        *,
        nonnegative: bool = False,
        default: str | None = None,
    ) -> Decimal:
        if key not in payload:
            if default is None:
                raise AccountSnapshotPayloadError(f"Missing account field: {key}")
            raw: Any = default
        else:
            raw = payload[key]
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise AccountSnapshotPayloadError(f"Invalid account field: {key}") from exc
        if not value.is_finite() or (nonnegative and value < 0):
            raise AccountSnapshotPayloadError(f"Invalid account field: {key}")
        return value


class SnapshotAwarePrivateClient:
    """Reuse AccountSnapshotService for account/positions/day-income and delegate other calls."""

    def __init__(
        self,
        client: SnapshotDelegatingPrivateClient,
        snapshots: AccountSnapshotService,
    ) -> None:
        self._client = client
        self._snapshots = snapshots

    def account(self) -> dict[str, Any]:
        snapshot = self._snapshots.get()
        return {
            "canTrade": snapshot.can_trade,
            "totalWalletBalance": format(snapshot.total_wallet_balance_usdt, "f"),
            "availableBalance": format(snapshot.available_balance_usdt, "f"),
            "totalUnrealizedProfit": format(snapshot.total_unrealized_pnl_usdt, "f"),
            "totalInitialMargin": format(snapshot.total_initial_margin_usdt, "f"),
            "assets": [
                {
                    "asset": item.asset,
                    "walletBalance": format(item.wallet_balance, "f"),
                    "availableBalance": format(item.available_balance, "f"),
                    "unrealizedProfit": format(item.unrealized_pnl, "f"),
                }
                for item in snapshot.balances
            ],
        }

    def positions(self) -> list[dict[str, Any]]:
        snapshot = self._snapshots.get()
        return [
            {
                "symbol": item.symbol,
                "positionAmt": format(item.position_amount, "f"),
                "leverage": str(item.leverage),
                "entryPrice": format(item.entry_price, "f"),
                "unRealizedProfit": format(item.unrealized_pnl, "f"),
            }
            for item in snapshot.positions
        ]

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        snapshot = self._snapshots.get()
        day_start = snapshot.captured_at.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_ms = int(day_start.timestamp() * 1000)
        captured_ms = int(snapshot.captured_at.timestamp() * 1000)
        if start_time_ms == day_start_ms and end_time_ms <= captured_ms + 60_000 and limit == 1000:
            return [
                {
                    "incomeType": item.income_type,
                    "income": format(item.income, "f"),
                    **({"time": item.time_ms} if item.time_ms is not None else {}),
                }
                for item in snapshot.income
            ]
        return self._client.income_history(
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            limit=limit,
        )

    def exchange_info(self) -> dict[str, Any]:
        return self._client.exchange_info()

    def mark_price(self, symbol: str) -> dict[str, Any]:
        return self._client.mark_price(symbol)

    def position_mode(self) -> dict[str, Any]:
        return self._client.position_mode()

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        return self._client.query_order(symbol=symbol, orig_client_order_id=orig_client_order_id)

    def query_algo_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        return self._client.query_algo_order(
            symbol=symbol,
            orig_client_order_id=orig_client_order_id,
        )

    def cancel_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        return self._client.cancel_order(symbol=symbol, orig_client_order_id=orig_client_order_id)

    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str,
        new_client_order_id: str,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        result = self._client.place_market_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            new_client_order_id=new_client_order_id,
            reduce_only=reduce_only,
        )
        self._snapshots.invalidate()
        return result

    def place_protective_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str,
        stop_price: str,
        new_client_order_id: str,
    ) -> dict[str, Any]:
        return self._client.place_protective_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            stop_price=stop_price,
            new_client_order_id=new_client_order_id,
        )

    def open_orders(self) -> list[dict[str, Any]]:
        return self._client.open_orders()

    def open_algo_orders(self) -> list[dict[str, Any]]:
        return self._client.open_algo_orders()


class FreshAccountExecutionService(DemoExecutionService):
    """Require a newly proven account snapshot before any enabled new-entry execution."""

    def __init__(
        self,
        inner: DemoExecutionService,
        snapshots: AccountSnapshotService,
        *,
        refresh_required: bool,
    ) -> None:
        self._inner = inner
        self._snapshots = snapshots
        self._refresh_required = refresh_required

    def _require_fresh_account(self) -> None:
        if not self._refresh_required:
            return
        try:
            self._snapshots.force_refresh()
        except (BinanceDemoPrivateClientError, AccountSnapshotPayloadError) as exc:
            raise AppError(
                status_code=503,
                code="ACCOUNT_SNAPSHOT_REFRESH_FAILED",
                message=(
                    "Fresh Binance Demo account state could not be proven; "
                    "new entry is blocked"
                ),
            ) from exc

    def status(self) -> DemoExecutionStatusResponse:
        return self._inner.status()

    def plans(self) -> DemoExecutionPlanList:
        return self._inner.plans()

    def trades(self) -> DemoTradeRecordList:
        return self._inner.trades()

    def account(self) -> DemoExecutionAccountResponse:
        return self._inner.account()

    def auto_execute_pending(self) -> int:
        self._require_fresh_account()
        return self._inner.auto_execute_pending()

    def activate(
        self,
        signal_id: str,
        request: DemoExecutionActivateRequest | None = None,
    ) -> DemoTradeRecord:
        self._require_fresh_account()
        return self._inner.activate(signal_id, request)

    def get_trade(self, trade_id: str) -> DemoTradeRecord | None:
        return self._inner.get_trade(trade_id)

    def store_trade(self, trade: DemoTradeRecord) -> DemoTradeRecord:
        return self._inner.store_trade(trade)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def to_execution_account_response(snapshot: AccountSnapshot) -> DemoExecutionAccountResponse:
    """Project one shared account snapshot into the existing execution API contract."""

    balances = [
        DemoAccountBalance(
            asset=item.asset,
            wallet_balance=item.wallet_balance,
            available_balance=item.available_balance,
            unrealized_pnl=item.unrealized_pnl,
        )
        for item in snapshot.balances
        if item.wallet_balance != 0 or item.available_balance != 0 or item.unrealized_pnl != 0
    ]
    positions = [
        DemoPositionSnapshot(
            symbol=item.symbol,
            side=(
                ScannerDirection.LONG
                if item.position_amount > 0
                else ScannerDirection.SHORT
            ),
            quantity=abs(item.position_amount),
            entry_price=item.entry_price,
            unrealized_pnl=item.unrealized_pnl,
        )
        for item in snapshot.positions
        if item.position_amount != 0
    ]
    return DemoExecutionAccountResponse(
        demo_private_execution_ready=True,
        can_trade=snapshot.can_trade,
        updated_at=snapshot.captured_at,
        total_wallet_balance_usdt=snapshot.total_wallet_balance_usdt,
        available_balance_usdt=snapshot.available_balance_usdt,
        total_unrealized_pnl_usdt=snapshot.total_unrealized_pnl_usdt,
        balances=balances,
        open_positions=positions,
    )
