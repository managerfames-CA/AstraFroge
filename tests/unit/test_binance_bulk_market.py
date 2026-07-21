"""Bulk Binance market-data client tests."""

from __future__ import annotations

import httpx
import pytest

from app.integrations.binance.public_client import BinancePublicClient, BinancePublicClientError


@pytest.mark.anyio
async def test_bulk_ticker_and_book_ticker_payloads() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ticker/24hr"):
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT", "quoteVolume": "10000000"}],
                request=request,
            )
        return httpx.Response(
            200,
            json=[{"symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "100.01"}],
            request=request,
        )

    client = BinancePublicClient(
        base_url="https://fapi.binance.com",
        timeout_seconds=1,
        retry_attempts=1,
        transport=httpx.MockTransport(handler),
    )

    tickers = await client.ticker_24h_all()
    books = await client.book_tickers()

    assert tickers[0]["symbol"] == "BTCUSDT"
    assert books[0]["bidPrice"] == "100"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "method_name", "match"),
    [
        ("/fapi/v1/ticker/24hr", "ticker_24h_all", "bulk ticker"),
        ("/fapi/v1/ticker/bookTicker", "book_tickers", "book-ticker"),
    ],
)
async def test_bulk_payload_rejects_non_object_rows(
    path: str,
    method_name: str,
    match: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == path
        return httpx.Response(200, json=["invalid"], request=request)

    client = BinancePublicClient(
        base_url="https://fapi.binance.com",
        timeout_seconds=1,
        retry_attempts=1,
        transport=httpx.MockTransport(handler),
    )

    method = getattr(client, method_name)
    with pytest.raises(BinancePublicClientError, match=match):
        await method()
