"""Unit tests for the Binance demo private client."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from app.integrations.binance.private_demo_client import (
    BinanceDemoPrivateClient,
    BinanceDemoPrivateClientError,
)


def test_private_demo_client_signs_and_sends_order() -> None:
    captured_query: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_query
        captured_query = request.url.query.decode()
        return httpx.Response(
            200,
            json={
                "orderId": 1,
                "clientOrderId": "abc",
                "status": "NEW",
                "executedQty": "0",
                "avgPrice": "0",
            },
        )

    client = BinanceDemoPrivateClient(
        base_url="https://demo-fapi.binance.example",
        api_key="demo-key",
        api_secret="demo-secret",
        timeout_seconds=5,
        recv_window_ms=5000,
        transport=httpx.MockTransport(handler),
    )

    payload = client.place_market_order(
        symbol="BTCUSDT",
        side="BUY",
        quantity="0.01",
        new_client_order_id="abc",
    )

    assert payload["status"] == "NEW"
    assert captured_query is not None
    parsed = parse_qs(captured_query)
    assert parsed["symbol"] == ["BTCUSDT"]
    assert parsed["signature"]


def test_private_demo_client_hides_raw_error_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": -2015, "msg": "Invalid API-key"})

    client = BinanceDemoPrivateClient(
        base_url="https://demo-fapi.binance.example",
        api_key="demo-key",
        api_secret="demo-secret",
        timeout_seconds=5,
        recv_window_ms=5000,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(BinanceDemoPrivateClientError, match="exchange code -2015"):
        client.account()
