"""Coverage and contract tests for Phase 2 pooled clients and snapshot adapters."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.core.errors import AppError
from app.integrations.binance.pooled_clients import (
    PooledBinanceDemoRecoveryClient,
    PooledBinancePublicClient,
)
from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.services.account_snapshot import (
    AccountSnapshotPayloadError,
    AccountSnapshotService,
    FreshAccountExecutionService,
    SnapshotAwarePrivateClient,
    to_execution_account_response,
)


class FullPrivateFake:
    def __init__(self) -> None:
        self.market_orders = 0

    def account(self) -> dict[str, Any]:
        return {
            "canTrade": True,
            "totalWalletBalance": "1000",
            "availableBalance": "800",
            "totalUnrealizedProfit": "4",
            "totalInitialMargin": "100",
            "assets": [
                {
                    "asset": "USDT",
                    "walletBalance": "1000",
                    "availableBalance": "800",
                    "unrealizedProfit": "4",
                }
            ],
        }

    def positions(self) -> list[dict[str, Any]]:
        return [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.01",
                "leverage": "3",
                "entryPrice": "65000",
                "unRealizedProfit": "4",
            }
        ]

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return [{"incomeType": "REALIZED_PNL", "income": "2", "time": end_time_ms}]

    def exchange_info(self) -> dict[str, Any]:
        return {"symbols": []}

    def mark_price(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "markPrice": "65000"}

    def position_mode(self) -> dict[str, Any]:
        return {"dualSidePosition": False}

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        return {"symbol": symbol, "clientOrderId": orig_client_order_id, "status": "FILLED"}

    def query_algo_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        return {"symbol": symbol, "clientOrderId": orig_client_order_id, "status": "NEW"}

    def cancel_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        return {"symbol": symbol, "clientOrderId": orig_client_order_id, "status": "CANCELED"}

    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str,
        new_client_order_id: str,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        self.market_orders += 1
        return {
            "symbol": symbol,
            "side": side,
            "origQty": quantity,
            "clientOrderId": new_client_order_id,
            "reduceOnly": reduce_only,
            "status": "FILLED",
        }

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
        return {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "origQty": quantity,
            "stopPrice": stop_price,
            "clientOrderId": new_client_order_id,
        }

    def open_orders(self) -> list[dict[str, Any]]:
        return []

    def open_algo_orders(self) -> list[dict[str, Any]]:
        return []


def test_snapshot_proxy_delegates_non_snapshot_methods_and_invalidates_after_market_order() -> None:
    raw = FullPrivateFake()
    snapshots = AccountSnapshotService(raw, freshness_seconds=60)
    proxy = SnapshotAwarePrivateClient(raw, snapshots)
    initial = snapshots.get()

    assert proxy.exchange_info() == {"symbols": []}
    assert proxy.mark_price("BTCUSDT")["markPrice"] == "65000"
    assert proxy.position_mode()["dualSidePosition"] is False
    assert proxy.query_order(symbol="BTCUSDT", orig_client_order_id="entry")["status"] == "FILLED"
    assert proxy.query_algo_order(symbol="BTCUSDT", orig_client_order_id="sl")["status"] == "NEW"
    assert proxy.cancel_order(symbol="BTCUSDT", orig_client_order_id="sl")["status"] == "CANCELED"
    assert proxy.open_orders() == []
    assert proxy.open_algo_orders() == []
    assert (
        proxy.place_protective_order(
            symbol="BTCUSDT",
            side="SELL",
            order_type="STOP_MARKET",
            quantity="0.01",
            stop_price="64000",
            new_client_order_id="sl",
        )["type"]
        == "STOP_MARKET"
    )

    proxy.place_market_order(
        symbol="BTCUSDT",
        side="BUY",
        quantity="0.01",
        new_client_order_id="entry",
    )
    assert raw.market_orders == 1
    assert snapshots.status().snapshot_id is None
    refreshed = snapshots.get()
    assert refreshed.snapshot_id != initial.snapshot_id


def test_account_snapshot_projects_to_existing_execution_account_contract() -> None:
    snapshot = AccountSnapshotService(FullPrivateFake(), freshness_seconds=60).get()
    response = to_execution_account_response(snapshot)

    assert response.can_trade is True
    assert response.total_wallet_balance_usdt == 1000
    assert len(response.balances) == 1
    assert response.open_positions[0].symbol == "BTCUSDT"
    assert response.open_positions[0].quantity == Decimal("0.01")


def test_account_snapshot_rejects_invalid_constructor_and_payloads() -> None:
    with pytest.raises(ValueError, match="freshness"):
        AccountSnapshotService(FullPrivateFake(), freshness_seconds=-1)

    bad = FullPrivateFake()
    bad.account = lambda: {  # type: ignore[method-assign]
        "canTrade": "yes",
        "totalWalletBalance": "1000",
        "availableBalance": "800",
        "totalUnrealizedProfit": "0",
        "totalInitialMargin": "0",
        "assets": [],
    }
    with pytest.raises(AccountSnapshotPayloadError, match="canTrade"):
        AccountSnapshotService(bad).get()


class InnerExecutionFake:
    def __init__(self) -> None:
        self.auto_calls = 0
        self.activate_calls = 0

    def auto_execute_pending(self) -> int:
        self.auto_calls += 1
        return 2

    def activate(self, signal_id: str, request: Any = None) -> str:
        self.activate_calls += 1
        return signal_id


def test_fresh_account_execution_guard_allows_valid_refresh_before_new_entry() -> None:
    snapshots = AccountSnapshotService(FullPrivateFake(), freshness_seconds=60)
    inner = InnerExecutionFake()
    guarded = FreshAccountExecutionService(
        inner,  # type: ignore[arg-type]
        snapshots,
        refresh_required=True,
    )

    assert guarded.auto_execute_pending() == 2
    assert inner.auto_calls == 1
    assert snapshots.status().refresh_count == 1


def test_fresh_account_execution_guard_fails_closed_on_private_refresh_failure() -> None:
    class FailingPrivate(FullPrivateFake):
        def account(self) -> dict[str, Any]:
            raise BinanceDemoPrivateClientError("unavailable")

    snapshots = AccountSnapshotService(FailingPrivate(), freshness_seconds=60)
    guarded = FreshAccountExecutionService(
        InnerExecutionFake(),  # type: ignore[arg-type]
        snapshots,
        refresh_required=True,
    )

    with pytest.raises(AppError) as exc_info:
        guarded.activate("signal-1")
    assert exc_info.value.code == "ACCOUNT_SNAPSHOT_REFRESH_FAILED"


def test_pooled_public_client_reuses_one_client_for_multiple_requests() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/time"):
            return httpx.Response(200, json={"serverTime": 1_700_000_000_000})
        return httpx.Response(
            200,
            json={
                "symbol": "BTCUSDT",
                "lastPrice": "1",
                "priceChangePercent": "0",
                "highPrice": "1",
                "lowPrice": "1",
                "quoteVolume": "1",
                "closeTime": 1_700_000_000_000,
            },
        )

    async def scenario() -> None:
        client = PooledBinancePublicClient(
            base_url="https://example.test",
            timeout_seconds=1,
            transport=httpx.MockTransport(handler),
        )
        await client.exchange_time()
        await client.ticker_24h("BTCUSDT")
        await client.aclose()

    asyncio.run(scenario())
    assert calls == ["/fapi/v1/time", "/fapi/v1/ticker/24hr"]


def test_pooled_private_client_reuses_one_client_and_preserves_signed_methods() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["X-MBX-APIKEY"] == "key"
        assert "signature=" in str(request.url)
        if request.url.path.endswith("/account"):
            return httpx.Response(200, json={"canTrade": True})
        if request.url.path.endswith("/positionRisk"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])

    client = PooledBinanceDemoRecoveryClient(
        base_url="https://demo.example.test",
        api_key="key",
        api_secret="secret",
        timeout_seconds=1,
        recv_window_ms=5000,
        transport=httpx.MockTransport(handler),
    )
    assert client.account()["canTrade"] is True
    assert client.positions() == []
    assert client.open_orders() == []
    client.close()

    assert calls == ["/fapi/v2/account", "/fapi/v2/positionRisk", "/fapi/v1/openOrders"]


def test_pooled_clients_preserve_error_mapping() -> None:
    def public_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": -1})

    async def public_scenario() -> None:
        client = PooledBinancePublicClient(
            base_url="https://example.test",
            timeout_seconds=1,
            retry_attempts=1,
            transport=httpx.MockTransport(public_handler),
        )
        with pytest.raises(Exception, match="status 400"):
            await client.exchange_time()
        await client.aclose()

    asyncio.run(public_scenario())

    def private_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": -2013})

    private = PooledBinanceDemoRecoveryClient(
        base_url="https://demo.example.test",
        api_key="key",
        api_secret="secret",
        timeout_seconds=1,
        recv_window_ms=5000,
        transport=httpx.MockTransport(private_handler),
    )
    with pytest.raises(BinanceDemoPrivateClientError) as exc_info:
        private.account()
    assert exc_info.value.status_code == 400
    assert exc_info.value.exchange_code == -2013
    private.close()
