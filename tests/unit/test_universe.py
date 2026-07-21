"""Universe Engine unit tests."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.services.universe import UniverseService


class FakeUniverseClient:
    def __init__(
        self,
        symbols: list[Any],
        tickers: list[dict[str, Any]],
        books: list[dict[str, Any]],
    ) -> None:
        self._symbols = symbols
        self._tickers = tickers
        self._books = books

    async def exchange_info(self) -> dict[str, Any]:
        return {"symbols": self._symbols}

    async def ticker_24h_all(self) -> list[dict[str, Any]]:
        return self._tickers

    async def book_tickers(self) -> list[dict[str, Any]]:
        return self._books


def _symbol(
    name: str,
    *,
    base: str = "BTC",
    quote: str = "USDT",
    contract: str = "PERPETUAL",
    status: str = "TRADING",
) -> dict[str, Any]:
    return {
        "symbol": name,
        "baseAsset": base,
        "quoteAsset": quote,
        "contractType": contract,
        "status": status,
    }


@pytest.mark.anyio
async def test_ranks_by_volume_then_caps_universe() -> None:
    client = FakeUniverseClient(
        symbols=[_symbol("BTCUSDT"), _symbol("ETHUSDT", base="ETH")],
        tickers=[
            {"symbol": "BTCUSDT", "quoteVolume": "20000000"},
            {"symbol": "ETHUSDT", "quoteVolume": "30000000"},
        ],
        books=[
            {"symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "100.02"},
            {"symbol": "ETHUSDT", "bidPrice": "50", "askPrice": "50.005"},
        ],
    )
    service = UniverseService(
        client,
        max_symbols=1,
        min_quote_volume=Decimal("10000000"),
        max_spread_bps=Decimal("10"),
    )

    snapshot = await service.build()

    assert [item.symbol for item in snapshot.candidates] == ["ETHUSDT"]
    assert snapshot.candidates[0].rank == 1
    assert snapshot.candidates[0].quote_volume == Decimal("30000000")
    assert [(item.symbol, item.code) for item in snapshot.rejections] == [
        ("BTCUSDT", "universe_limit")
    ]
    assert snapshot.eligible_count == 1
    assert snapshot.rejected_count == 1


@pytest.mark.anyio
async def test_records_deterministic_rejection_reasons() -> None:
    symbols: list[Any] = [
        "invalid-row",
        _symbol("BTCUSDC", quote="USDC"),
        _symbol("BTCUSDT_250101", contract="CURRENT_QUARTER"),
        _symbol("HALTUSDT", status="SETTLING"),
        _symbol("NOBASEUSDT", base=""),
        _symbol("NOTICKUSDT"),
        _symbol("BADVOLUSDT"),
        _symbol("LOWUSDT"),
        _symbol("NOBOOKUSDT"),
        _symbol("BADBOOKUSDT"),
        _symbol("WIDEUSDT"),
        _symbol("GOODUSDT"),
        _symbol("GOODUSDT"),
    ]
    tickers = [
        {"symbol": "BADVOLUSDT", "quoteVolume": "invalid"},
        {"symbol": "LOWUSDT", "quoteVolume": "999"},
        {"symbol": "NOBOOKUSDT", "quoteVolume": "20000000"},
        {"symbol": "BADBOOKUSDT", "quoteVolume": "20000000"},
        {"symbol": "WIDEUSDT", "quoteVolume": "20000000"},
        {"symbol": "GOODUSDT", "quoteVolume": "20000000"},
    ]
    books = [
        {"symbol": "BADBOOKUSDT", "bidPrice": "101", "askPrice": "100"},
        {"symbol": "WIDEUSDT", "bidPrice": "100", "askPrice": "101"},
        {"symbol": "GOODUSDT", "bidPrice": "100", "askPrice": "100.01"},
    ]
    service = UniverseService(
        FakeUniverseClient(symbols, tickers, books),
        max_symbols=50,
        min_quote_volume=Decimal("1000"),
        max_spread_bps=Decimal("10"),
    )

    snapshot = await service.build()

    assert [item.symbol for item in snapshot.candidates] == ["GOODUSDT"]
    codes = {(item.symbol, item.code) for item in snapshot.rejections}
    assert ("<unknown:0>", "invalid_symbol_metadata") in codes
    assert ("BTCUSDC", "non_usdt_quote") in codes
    assert ("BTCUSDT_250101", "non_perpetual_contract") in codes
    assert ("HALTUSDT", "not_trading") in codes
    assert ("NOBASEUSDT", "invalid_symbol_metadata") in codes
    assert ("NOTICKUSDT", "missing_ticker") in codes
    assert ("BADVOLUSDT", "invalid_quote_volume") in codes
    assert ("LOWUSDT", "below_min_quote_volume") in codes
    assert ("NOBOOKUSDT", "missing_book_ticker") in codes
    assert ("BADBOOKUSDT", "invalid_book_ticker") in codes
    assert ("WIDEUSDT", "spread_too_wide") in codes
    assert ("GOODUSDT", "invalid_symbol_metadata") in codes


@pytest.mark.anyio
async def test_invalid_exchange_symbol_collection_is_rejected() -> None:
    class InvalidClient(FakeUniverseClient):
        async def exchange_info(self) -> dict[str, Any]:
            return {"symbols": "invalid"}

    service = UniverseService(
        InvalidClient([], [], []),
        max_symbols=50,
        min_quote_volume=Decimal("0"),
        max_spread_bps=Decimal("10"),
    )

    with pytest.raises(ValueError, match="Invalid exchange-info symbols payload"):
        await service.build()


@pytest.mark.anyio
async def test_hybrid_dynamic_50_balances_liquidity_momentum_and_activity() -> None:
    symbols: list[dict[str, Any]] = []
    tickers: list[dict[str, Any]] = []
    books: list[dict[str, Any]] = []

    def add(name: str, volume: int, change: int, count: int) -> None:
        symbols.append(_symbol(name, base=name.removesuffix("USDT")))
        tickers.append(
            {
                "symbol": name,
                "quoteVolume": str(volume),
                "priceChangePercent": str(change),
                "count": count,
            }
        )
        books.append({"symbol": name, "bidPrice": "100", "askPrice": "100.01"})

    for i in range(10):
        add(f"CORE{i:02d}USDT", 100_000_000 - i, 0, 1000 - i)
    for i in range(20):
        add(f"GAIN{i:02d}USDT", 30_000_000 - i, 40 - i, 2000 - i)
    for i in range(20):
        add(f"LOSS{i:02d}USDT", 29_000_000 - i, -40 + i, 1900 - i)
    for i in range(10):
        add(f"ACT{i:02d}USDT", 20_000_000 - i, 1, 100_000 - i)

    service = UniverseService(
        FakeUniverseClient(symbols, tickers, books),
        max_symbols=50,
        min_quote_volume=Decimal("10000000"),
        max_spread_bps=Decimal("10"),
    )

    snapshot = await service.build()
    selected = {item.symbol for item in snapshot.candidates}

    assert len(snapshot.candidates) == 50
    assert {f"CORE{i:02d}USDT" for i in range(10)} <= selected
    assert {f"GAIN{i:02d}USDT" for i in range(15)} <= selected
    assert {f"LOSS{i:02d}USDT" for i in range(15)} <= selected
    assert {f"ACT{i:02d}USDT" for i in range(10)} <= selected
    assert [item.rank for item in snapshot.candidates] == list(range(1, 51))
