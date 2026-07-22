"""Sync Binance USD-M Futures demo/testnet private REST client."""

from __future__ import annotations

import hashlib
import hmac
from time import time
from typing import Any
from urllib.parse import urlencode

import httpx


class BinanceDemoPrivateClientError(RuntimeError):
    """Stable adapter error that never exposes raw secrets or unsafe payloads."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        exchange_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.exchange_code = exchange_code


class BinanceDemoPrivateClient:
    """Signed client for Binance USD-M Futures Demo execution flows."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        timeout_seconds: float,
        recv_window_ms: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._recv_window_ms = recv_window_ms
        self._transport = transport

    def _signed_params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(params or {})
        payload["recvWindow"] = self._recv_window_ms
        payload["timestamp"] = int(time() * 1000)
        query = urlencode(payload, doseq=True)
        signature = hmac.new(
            self._api_secret,
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        payload["signature"] = signature
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = True,
    ) -> Any:
        request_params = self._signed_params(params) if signed else dict(params or {})
        headers = {"X-MBX-APIKEY": self._api_key}
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                headers=headers,
                transport=self._transport,
            ) as client:
                response = client.request(method, path, params=request_params)
                response.raise_for_status()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise BinanceDemoPrivateClientError(
                "Binance demo private API is unavailable"
            ) from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            exchange_code: int | None = None
            try:
                payload = exc.response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("code"), int):
                exchange_code = payload["code"]
            message = f"Binance demo private API request failed with status {status_code}"
            if exchange_code is not None:
                message += f" and exchange code {exchange_code}"
            raise BinanceDemoPrivateClientError(
                message,
                status_code=status_code,
                exchange_code=exchange_code,
            ) from exc

        try:
            return response.json()
        except ValueError as exc:
            raise BinanceDemoPrivateClientError(
                "Binance demo private API returned invalid JSON"
            ) from exc

    @staticmethod
    def _dict_payload(payload: Any, *, name: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise BinanceDemoPrivateClientError(f"Unexpected {name} response")
        return payload

    @staticmethod
    def _normalized_algo_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Expose conditional Algo responses through the existing order identity contract."""

        normalized = dict(payload)
        if "clientOrderId" not in normalized and normalized.get("clientAlgoId") is not None:
            normalized["clientOrderId"] = normalized["clientAlgoId"]
        if "orderId" not in normalized and normalized.get("algoId") is not None:
            normalized["orderId"] = normalized["algoId"]
        return normalized

    def exchange_info(self) -> dict[str, Any]:
        return self._dict_payload(
            self._request("GET", "/fapi/v1/exchangeInfo", signed=False),
            name="exchange info",
        )

    def mark_price(self, symbol: str) -> dict[str, Any]:
        return self._dict_payload(
            self._request(
                "GET",
                "/fapi/v1/premiumIndex",
                params={"symbol": symbol},
                signed=False,
            ),
            name="mark price",
        )

    def position_mode(self) -> dict[str, Any]:
        return self._dict_payload(
            self._request("GET", "/fapi/v1/positionSide/dual"),
            name="position mode",
        )

    def account(self) -> dict[str, Any]:
        return self._dict_payload(
            self._request("GET", "/fapi/v2/account"),
            name="account",
        )

    def positions(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/fapi/v2/positionRisk")
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise BinanceDemoPrivateClientError("Unexpected positions response")
        return payload

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return exchange-authoritative Demo income records for one bounded window."""

        payload = self._request(
            "GET",
            "/fapi/v1/income",
            params={
                "startTime": start_time_ms,
                "endTime": end_time_ms,
                "limit": limit,
            },
        )
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise BinanceDemoPrivateClientError("Unexpected income history response")
        return payload

    def query_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        return self._dict_payload(
            self._request(
                "GET",
                "/fapi/v1/order",
                params={
                    "symbol": symbol,
                    "origClientOrderId": orig_client_order_id,
                },
            ),
            name="order query",
        )

    def query_algo_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        payload = self._dict_payload(
            self._request(
                "GET",
                "/fapi/v1/algoOrder",
                params={
                    "symbol": symbol,
                    "clientAlgoId": orig_client_order_id,
                },
            ),
            name="algo order query",
        )
        return self._normalized_algo_payload(payload)

    def cancel_order(self, *, symbol: str, orig_client_order_id: str) -> dict[str, Any]:
        payload = self._dict_payload(
            self._request(
                "DELETE",
                "/fapi/v1/algoOrder",
                params={
                    "symbol": symbol,
                    "clientAlgoId": orig_client_order_id,
                },
            ),
            name="algo order cancellation",
        )
        return self._normalized_algo_payload(payload)

    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str,
        new_client_order_id: str,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "newClientOrderId": new_client_order_id,
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._dict_payload(
            self._request("POST", "/fapi/v1/order", params=params),
            name="market order",
        )

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
        if order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            raise ValueError("Unsupported protective order type")
        try:
            self._dict_payload(
                self._request(
                    "POST",
                    "/fapi/v1/algoOrder",
                    params={
                        "symbol": symbol,
                        "side": side,
                        "algoType": "CONDITIONAL",
                        "type": order_type,
                        "quantity": quantity,
                        "triggerPrice": stop_price,
                        "reduceOnly": "true",
                        "workingType": "MARK_PRICE",
                        "priceProtect": "TRUE",
                        "clientAlgoId": new_client_order_id,
                        "newOrderRespType": "RESULT",
                    },
                ),
                name="protective algo order",
            )
        except BinanceDemoPrivateClientError as submit_error:
            try:
                return self.query_algo_order(
                    symbol=symbol,
                    orig_client_order_id=new_client_order_id,
                )
            except BinanceDemoPrivateClientError as query_error:
                raise submit_error from query_error
        return self.query_algo_order(
            symbol=symbol,
            orig_client_order_id=new_client_order_id,
        )
