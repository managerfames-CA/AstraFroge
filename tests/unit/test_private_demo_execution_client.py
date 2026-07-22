"""Binance Demo execution adapter request-contract tests."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.integrations.binance.private_demo_client import (
    BinanceDemoPrivateClient,
    BinanceDemoPrivateClientError,
)


def _client(handler) -> BinanceDemoPrivateClient:  # type: ignore[no-untyped-def]
    return BinanceDemoPrivateClient(
        base_url="https://demo-fapi.binance.example",
        api_key="demo-key",
        api_secret="demo-secret",
        timeout_seconds=2,
        recv_window_ms=5000,
        transport=httpx.MockTransport(handler),
    )


def test_public_rule_and_mark_requests_are_unsigned() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/exchangeInfo"):
            return httpx.Response(200, json={"symbols": []})
        return httpx.Response(200, json={"symbol": "BTCUSDT", "markPrice": "100"})

    client = _client(handler)
    assert client.exchange_info() == {"symbols": []}
    assert client.mark_price("BTCUSDT")["markPrice"] == "100"

    assert "signature" not in requests[0].url.params
    assert "timestamp" not in requests[0].url.params
    assert requests[1].url.params["symbol"] == "BTCUSDT"
    assert "signature" not in requests[1].url.params


def test_market_and_protective_orders_publish_hardened_parameters() -> None:
    captured: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        captured.append((request.method, request.url.path, params))
        if request.url.path == "/fapi/v1/algoOrder":
            if request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "algoId": 3,
                        "clientAlgoId": params["clientAlgoId"],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "algoId": 3,
                    "clientAlgoId": params["clientAlgoId"],
                    "status": "NEW",
                },
            )
        return httpx.Response(
            200,
            json={
                "orderId": len(captured),
                "clientOrderId": params["newClientOrderId"],
                "status": "NEW",
            },
        )

    client = _client(handler)
    client.place_market_order(
        symbol="BTCUSDT",
        side="BUY",
        quantity="0.1",
        new_client_order_id="af-e-example",
    )
    client.place_market_order(
        symbol="BTCUSDT",
        side="SELL",
        quantity="0.1",
        new_client_order_id="af-x-example",
        reduce_only=True,
    )
    protective = client.place_protective_order(
        symbol="BTCUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        quantity="0.1",
        stop_price="95",
        new_client_order_id="af-s-example",
    )

    assert captured[0][1] == "/fapi/v1/order"
    assert captured[1][1] == "/fapi/v1/order"
    assert captured[2][0:2] == ("POST", "/fapi/v1/algoOrder")
    assert captured[3][0:2] == ("GET", "/fapi/v1/algoOrder")
    assert captured[0][2]["type"] == "MARKET"
    assert captured[0][2]["newOrderRespType"] == "RESULT"
    assert "reduceOnly" not in captured[0][2]
    assert captured[1][2]["reduceOnly"] == "true"
    assert captured[2][2]["algoType"] == "CONDITIONAL"
    assert captured[2][2]["type"] == "STOP_MARKET"
    assert captured[2][2]["reduceOnly"] == "true"
    assert captured[2][2]["workingType"] == "MARK_PRICE"
    assert captured[2][2]["priceProtect"] == "TRUE"
    assert captured[2][2]["triggerPrice"] == "95"
    assert "stopPrice" not in captured[2][2]
    assert captured[2][2]["clientAlgoId"] == "af-s-example"
    assert captured[2][2]["newOrderRespType"] == "RESULT"
    assert captured[3][2]["clientAlgoId"] == "af-s-example"
    assert protective["clientOrderId"] == "af-s-example"
    assert protective["orderId"] == 3
    assert protective["status"] == "NEW"
    assert all("signature" in params and "timestamp" in params for _, _, params in captured)


def test_protective_submit_unknown_state_recovers_from_algo_query() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                500,
                json={"code": -1000, "msg": "unknown submission state"},
                request=request,
            )
        return httpx.Response(
            200,
            json={"algoId": 91, "clientAlgoId": "af-s-recover", "status": "NEW"},
        )

    protective = _client(handler).place_protective_order(
        symbol="BTCUSDT",
        side="SELL",
        order_type="STOP_MARKET",
        quantity="0.1",
        stop_price="95",
        new_client_order_id="af-s-recover",
    )

    assert calls == ["POST", "GET"]
    assert protective["orderId"] == 91
    assert protective["clientOrderId"] == "af-s-recover"


def test_protective_ack_is_not_accepted_without_query_confirmation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"success": True, "algoId": 7, "clientAlgoId": "af-s-unconfirmed"},
            )
        return httpx.Response(
            404,
            json={"code": -2013, "msg": "Order does not exist"},
            request=request,
        )

    with pytest.raises(BinanceDemoPrivateClientError) as exc:
        _client(handler).place_protective_order(
            symbol="BTCUSDT",
            side="SELL",
            order_type="STOP_MARKET",
            quantity="0.1",
            stop_price="95",
            new_client_order_id="af-s-unconfirmed",
        )

    assert exc.value.exchange_code == -2013


def test_query_and_cancel_use_deterministic_client_order_id() -> None:
    methods: list[str] = []
    paths: list[str] = []
    params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        paths.append(request.url.path)
        params.append(dict(request.url.params))
        if request.url.path.endswith("/algoOrder"):
            return httpx.Response(
                200,
                json={"algoId": 1, "clientAlgoId": "af-s-example", "status": "NEW"},
            )
        return httpx.Response(
            200,
            json={"orderId": 1, "clientOrderId": "af-e-example", "status": "FILLED"},
        )

    client = _client(handler)
    client.query_order(symbol="BTCUSDT", orig_client_order_id="af-e-example")
    client.query_algo_order(symbol="BTCUSDT", orig_client_order_id="af-s-example")
    client.cancel_order(symbol="BTCUSDT", orig_client_order_id="af-s-example")

    assert methods == ["GET", "GET", "DELETE"]
    assert paths == ["/fapi/v1/order", "/fapi/v1/algoOrder", "/fapi/v1/algoOrder"]
    assert params[0]["origClientOrderId"] == "af-e-example"
    assert params[1]["clientAlgoId"] == "af-s-example"
    assert params[2]["clientAlgoId"] == "af-s-example"
    assert "origClientAlgoId" not in params[1]
    assert "origClientAlgoId" not in params[2]


def test_exchange_error_exposes_only_sanitized_codes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"code": -2013, "msg": "sensitive exchange detail"},
            request=request,
        )

    client = _client(handler)
    with pytest.raises(BinanceDemoPrivateClientError) as exc:
        client.query_order(symbol="BTCUSDT", orig_client_order_id="af-e-example")

    assert exc.value.status_code == 400
    assert exc.value.exchange_code == -2013
    assert "sensitive exchange detail" not in str(exc.value)
