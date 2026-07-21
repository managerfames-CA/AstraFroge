"""Read-only Binance Demo recovery client tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.integrations.binance.recovery_demo_client import BinanceDemoRecoveryClient


class _RecoveryClient(BinanceDemoRecoveryClient):
    def __init__(self, payloads: dict[str, Any]) -> None:
        self.payloads = payloads
        self.paths: list[str] = []
        self.params: list[dict[str, Any] | None] = []

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = True,
    ) -> Any:
        del method, signed
        self.paths.append(path)
        self.params.append(params)
        return self.payloads[path]


def test_open_order_snapshots_use_account_wide_read_endpoints() -> None:
    client = _RecoveryClient(
        {
            "/fapi/v1/openOrders": [{"symbol": "BTCUSDT", "orderId": 1}],
            "/fapi/v1/openAlgoOrders": [
                {
                    "symbol": "BTCUSDT",
                    "clientAlgoId": "stop-1",
                    "algoId": 2,
                    "algoStatus": "NEW",
                }
            ],
        }
    )

    assert client.open_orders()[0]["orderId"] == 1
    algo = client.open_algo_orders()[0]
    assert algo["clientOrderId"] == "stop-1"
    assert algo["orderId"] == 2
    assert algo["status"] == "NEW"
    assert client.paths == [
        "/fapi/v1/openOrders",
        "/fapi/v1/openAlgoOrders",
    ]


def test_user_trades_uses_bounded_symbol_fill_endpoint() -> None:
    client = _RecoveryClient(
        {
            "/fapi/v1/userTrades": [
                {
                    "symbol": "BTCUSDT",
                    "id": 11,
                    "orderId": 22,
                    "qty": "0.01",
                    "price": "65000",
                }
            ]
        }
    )

    fills = client.user_trades(
        symbol="BTCUSDT",
        start_time_ms=100,
        end_time_ms=200,
        limit=500,
    )

    assert fills[0]["id"] == 11
    assert client.paths == ["/fapi/v1/userTrades"]
    assert client.params == [
        {
            "symbol": "BTCUSDT",
            "startTime": 100,
            "endTime": 200,
            "limit": 500,
        }
    ]


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("open_orders", "/fapi/v1/openOrders", {}),
        ("open_algo_orders", "/fapi/v1/openAlgoOrders", {}),
        (
            "user_trades",
            "/fapi/v1/userTrades",
            {
                "symbol": "BTCUSDT",
                "start_time_ms": 100,
                "end_time_ms": 200,
            },
        ),
    ],
)
def test_read_snapshots_reject_malformed_payload(
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    client = _RecoveryClient({path: {"unexpected": "object"}})

    with pytest.raises(BinanceDemoPrivateClientError):
        getattr(client, method)(**kwargs)
