"""Read-only Binance Demo recovery extensions for startup reconciliation."""

from __future__ import annotations

from typing import Any

from app.integrations.binance.private_demo_client import (
    BinanceDemoPrivateClient,
    BinanceDemoPrivateClientError,
)


class BinanceDemoRecoveryClient(BinanceDemoPrivateClient):
    """Expose account-wide read-only order, fill and recovery snapshots."""

    @staticmethod
    def _list_payload(payload: Any, *, name: str) -> list[dict[str, Any]]:
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise BinanceDemoPrivateClientError(f"Unexpected {name} response")
        return payload

    @staticmethod
    def _normalized_algo_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Normalize current Binance Algo identity/status fields to the execution contract."""

        normalized = BinanceDemoPrivateClient._normalized_algo_payload(payload)
        if "status" not in normalized and normalized.get("algoStatus") is not None:
            normalized["status"] = normalized["algoStatus"]
        return normalized

    def query_algo_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        """Return Algo identity plus actual regular-order execution economics."""

        payload = super().query_algo_order(
            symbol=symbol,
            orig_client_order_id=orig_client_order_id,
        )
        actual_order_id = payload.get("actualOrderId")
        if actual_order_id in {None, ""} or payload.get("executedQty") not in {None, ""}:
            return payload
        actual = self._dict_payload(
            self._request(
                "GET",
                "/fapi/v1/order",
                params={"symbol": symbol, "orderId": actual_order_id},
            ),
            name="actual protective order query",
        )
        if str(actual.get("orderId", "")) != str(actual_order_id):
            raise BinanceDemoPrivateClientError("Unexpected actual protective order identity")
        enriched = dict(payload)
        enriched["executedQty"] = actual.get("executedQty")
        if enriched.get("avgPrice") in {None, ""}:
            enriched["avgPrice"] = actual.get("avgPrice")
        return enriched

    def open_orders(self) -> list[dict[str, Any]]:
        """Return all currently open regular Demo orders for recovery checks."""

        return self._list_payload(
            self._request("GET", "/fapi/v1/openOrders"),
            name="open orders",
        )

    def open_algo_orders(self) -> list[dict[str, Any]]:
        """Return all currently open conditional Algo orders in normalized form."""

        payload = self._list_payload(
            self._request("GET", "/fapi/v1/openAlgoOrders"),
            name="open algo orders",
        )
        return [self._normalized_algo_payload(item) for item in payload]

    def user_trades(
        self,
        *,
        symbol: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return exchange-authoritative fills for one symbol and bounded time window."""

        return self._list_payload(
            self._request(
                "GET",
                "/fapi/v1/userTrades",
                params={
                    "symbol": symbol,
                    "startTime": start_time_ms,
                    "endTime": end_time_ms,
                    "limit": limit,
                },
            ),
            name="user trades",
        )
