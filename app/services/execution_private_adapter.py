"""Worker-safe Binance Demo private adapter for deterministic protection idempotency."""

from __future__ import annotations

from typing import Any

from app.integrations.binance.private_demo_client import BinanceDemoPrivateClientError
from app.services.account_snapshot import SnapshotAwarePrivateClient

_ORDER_NOT_FOUND = -2013


class QueryBeforeRetrySnapshotPrivateClient(SnapshotAwarePrivateClient):
    """Query deterministic protective identity before submit and after ambiguous failure."""

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
        existing = self._existing_algo_order(
            symbol=symbol,
            client_order_id=new_client_order_id,
        )
        if existing is not None:
            return existing

        try:
            return super().place_protective_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                stop_price=stop_price,
                new_client_order_id=new_client_order_id,
            )
        except BinanceDemoPrivateClientError as submission_error:
            recovered = self._existing_algo_order(
                symbol=symbol,
                client_order_id=new_client_order_id,
            )
            if recovered is not None:
                return recovered
            raise submission_error

    def _existing_algo_order(
        self,
        *,
        symbol: str,
        client_order_id: str,
    ) -> dict[str, Any] | None:
        try:
            return self.query_algo_order(
                symbol=symbol,
                orig_client_order_id=client_order_id,
            )
        except BinanceDemoPrivateClientError as exc:
            if exc.exchange_code == _ORDER_NOT_FOUND:
                return None
            raise
