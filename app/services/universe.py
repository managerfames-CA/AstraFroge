"""Deterministic public-market Universe Engine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.scanner.constants import UNIVERSE_REFRESH_INTERVAL
from app.schemas.universe import UniverseCandidate, UniverseRejection, UniverseSnapshot


class UniversePublicClient(Protocol):
    """Public methods required by the Universe Engine."""

    async def exchange_info(self) -> dict[str, Any]: ...

    async def ticker_24h_all(self) -> list[dict[str, Any]]: ...

    async def book_tickers(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class _EligibleRecord:
    symbol: str
    base_asset: str
    quote_volume: Decimal
    price_change_percent: Decimal
    trade_count: Decimal
    bid_price: Decimal
    ask_price: Decimal
    spread_bps: Decimal


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite():
        return None
    return result


def _index_by_symbol(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = row.get("symbol")
        if isinstance(symbol, str) and symbol:
            result[symbol] = row
    return result


class UniverseService:
    """Build a ranked and auditable universe from public Binance data."""

    def __init__(
        self,
        client: UniversePublicClient,
        *,
        max_symbols: int,
        min_quote_volume: Decimal,
        max_spread_bps: Decimal,
    ) -> None:
        self._client = client
        self._max_symbols = max_symbols
        self._min_quote_volume = min_quote_volume
        self._max_spread_bps = max_spread_bps
        self._cached_snapshot: UniverseSnapshot | None = None
        self._cached_at: datetime | None = None

    async def build(self) -> UniverseSnapshot:
        now = datetime.now(UTC)
        cached = self._cached_snapshot
        if (
            cached is not None
            and self._cached_at is not None
            and now - self._cached_at < UNIVERSE_REFRESH_INTERVAL
        ):
            return cached.model_copy(update={"generated_at": now})

        exchange_info, ticker_rows, book_rows = await asyncio.gather(
            self._client.exchange_info(),
            self._client.ticker_24h_all(),
            self._client.book_tickers(),
        )
        raw_symbols = exchange_info.get("symbols")
        if not isinstance(raw_symbols, list):
            raise ValueError("Invalid exchange-info symbols payload")

        tickers = _index_by_symbol(ticker_rows)
        books = _index_by_symbol(book_rows)
        eligible: list[_EligibleRecord] = []
        rejections: list[UniverseRejection] = []
        seen_symbols: set[str] = set()

        for index, raw_symbol in enumerate(raw_symbols):
            if not isinstance(raw_symbol, dict):
                rejections.append(
                    UniverseRejection(
                        symbol=f"<unknown:{index}>",
                        code="invalid_symbol_metadata",
                        detail="Symbol metadata must be an object",
                    )
                )
                continue

            raw_name = raw_symbol.get("symbol")
            symbol = raw_name if isinstance(raw_name, str) and raw_name else f"<unknown:{index}>"
            if symbol in seen_symbols or symbol.startswith("<unknown:"):
                rejections.append(
                    UniverseRejection(
                        symbol=symbol,
                        code="invalid_symbol_metadata",
                        detail="Symbol name is missing or duplicated",
                    )
                )
                continue
            seen_symbols.add(symbol)

            quote_asset = raw_symbol.get("quoteAsset")
            if quote_asset != "USDT":
                rejections.append(
                    UniverseRejection(
                        symbol=symbol,
                        code="non_usdt_quote",
                        detail="Quote asset is not USDT",
                    )
                )
                continue

            if raw_symbol.get("contractType") != "PERPETUAL":
                rejections.append(
                    UniverseRejection(
                        symbol=symbol,
                        code="non_perpetual_contract",
                        detail="Contract type is not PERPETUAL",
                    )
                )
                continue

            if raw_symbol.get("status") != "TRADING":
                rejections.append(
                    UniverseRejection(
                        symbol=symbol,
                        code="not_trading",
                        detail="Exchange status is not TRADING",
                    )
                )
                continue

            base_asset = raw_symbol.get("baseAsset")
            if not isinstance(base_asset, str) or not base_asset:
                rejections.append(
                    UniverseRejection(
                        symbol=symbol,
                        code="invalid_symbol_metadata",
                        detail="Base asset is missing",
                    )
                )
                continue

            ticker = tickers.get(symbol)
            if ticker is None:
                rejections.append(
                    UniverseRejection(
                        symbol=symbol,
                        code="missing_ticker",
                        detail="24-hour ticker is unavailable",
                    )
                )
                continue

            quote_volume = _decimal_or_none(ticker.get("quoteVolume"))
            if quote_volume is None or quote_volume < 0:
                rejections.append(
                    UniverseRejection(
                        symbol=symbol,
                        code="invalid_quote_volume",
                        detail="Quote volume is invalid",
                    )
                )
                continue
            if quote_volume < self._min_quote_volume:
                rejections.append(
                    UniverseRejection(
                        symbol=symbol,
                        code="below_min_quote_volume",
                        detail=f"Quote volume is below {self._min_quote_volume}",
                    )
                )
                continue

            price_change_percent = _decimal_or_none(ticker.get("priceChangePercent"))
            if price_change_percent is None:
                price_change_percent = Decimal("0")
            trade_count = _decimal_or_none(ticker.get("count"))
            if trade_count is None or trade_count < 0:
                trade_count = Decimal("0")

            book = books.get(symbol)
            if book is None:
                rejections.append(
                    UniverseRejection(
                        symbol=symbol,
                        code="missing_book_ticker",
                        detail="Best bid/ask snapshot is unavailable",
                    )
                )
                continue

            bid_price = _decimal_or_none(book.get("bidPrice"))
            ask_price = _decimal_or_none(book.get("askPrice"))
            if (
                bid_price is None
                or ask_price is None
                or bid_price <= 0
                or ask_price <= 0
                or ask_price < bid_price
            ):
                rejections.append(
                    UniverseRejection(
                        symbol=symbol,
                        code="invalid_book_ticker",
                        detail="Best bid/ask values are invalid",
                    )
                )
                continue

            midpoint = (bid_price + ask_price) / Decimal("2")
            spread_bps = ((ask_price - bid_price) / midpoint) * Decimal("10000")
            if spread_bps > self._max_spread_bps:
                rejections.append(
                    UniverseRejection(
                        symbol=symbol,
                        code="spread_too_wide",
                        detail=f"Spread exceeds {self._max_spread_bps} bps",
                    )
                )
                continue

            eligible.append(
                _EligibleRecord(
                    symbol=symbol,
                    base_asset=base_asset,
                    quote_volume=quote_volume,
                    price_change_percent=price_change_percent,
                    trade_count=trade_count,
                    bid_price=bid_price,
                    ask_price=ask_price,
                    spread_bps=spread_bps,
                )
            )

        liquidity_ranked = sorted(
            eligible,
            key=lambda item: (-item.quote_volume, item.spread_bps, item.symbol),
        )

        # Production Scanner uses a 50-symbol Hybrid Dynamic Universe:
        # 10 Core Liquid + 15 Top Gainers + 15 Top Losers + 10 Unusual Activity.
        # For smaller diagnostic/test caps, retain the historical liquidity ranking.
        if self._max_symbols >= 50:
            selected: list[_EligibleRecord] = []
            selected_symbols: set[str] = set()

            def add_unique(pool: list[_EligibleRecord], target: int) -> None:
                if target <= 0:
                    return
                added = 0
                for item in pool:
                    if item.symbol in selected_symbols:
                        continue
                    selected.append(item)
                    selected_symbols.add(item.symbol)
                    added += 1
                    if added >= target or len(selected) >= self._max_symbols:
                        return

            gainers_ranked = sorted(
                (item for item in eligible if item.price_change_percent > 0),
                key=lambda item: (
                    -item.price_change_percent,
                    -item.quote_volume,
                    item.spread_bps,
                    item.symbol,
                ),
            )
            losers_ranked = sorted(
                (item for item in eligible if item.price_change_percent < 0),
                key=lambda item: (
                    item.price_change_percent,
                    -item.quote_volume,
                    item.spread_bps,
                    item.symbol,
                ),
            )
            unusual_activity_ranked = sorted(
                eligible,
                key=lambda item: (
                    -item.trade_count,
                    -abs(item.price_change_percent),
                    -item.quote_volume,
                    item.spread_bps,
                    item.symbol,
                ),
            )

            add_unique(liquidity_ranked, 10)
            add_unique(gainers_ranked, 15)
            add_unique(losers_ranked, 15)
            add_unique(unusual_activity_ranked, 10)

            # If a bucket has too few eligible symbols, fill remaining slots with the
            # safest/highest-liquidity names so the universe remains deterministic.
            add_unique(liquidity_ranked, self._max_symbols - len(selected))
        else:
            selected = liquidity_ranked[: self._max_symbols]

        selected_symbols = {item.symbol for item in selected}
        for overflow in (item for item in liquidity_ranked if item.symbol not in selected_symbols):
            rejections.append(
                UniverseRejection(
                    symbol=overflow.symbol,
                    code="universe_limit",
                    detail=f"Rank is outside the maximum universe size of {self._max_symbols}",
                )
            )

        candidates = [
            UniverseCandidate(
                rank=rank,
                symbol=item.symbol,
                base_asset=item.base_asset,
                quote_volume=item.quote_volume,
                bid_price=item.bid_price,
                ask_price=item.ask_price,
                spread_bps=item.spread_bps,
            )
            for rank, item in enumerate(selected, start=1)
        ]
        ordered_rejections = sorted(rejections, key=lambda item: (item.symbol, item.code))
        snapshot = UniverseSnapshot(
            generated_at=now,
            max_symbols=self._max_symbols,
            min_quote_volume=self._min_quote_volume,
            max_spread_bps=self._max_spread_bps,
            eligible_count=len(candidates),
            rejected_count=len(ordered_rejections),
            candidates=candidates,
            rejections=ordered_rejections,
        )
        self._cached_snapshot = snapshot
        self._cached_at = now
        return snapshot
