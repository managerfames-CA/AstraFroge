"""Binance USD-M Futures symbol-rule parsing and exact Decimal normalization."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from typing import Any

from app.schemas.scanner import ScannerDirection


class ExchangeRuleError(ValueError):
    """Stable fail-closed exchange-rule validation error."""


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ExchangeRuleError(f"Invalid exchange rule field: {field}") from exc
    if not parsed.is_finite():
        raise ExchangeRuleError(f"Invalid exchange rule field: {field}")
    return parsed


def _filter_map(symbol_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    filters = symbol_payload.get("filters")
    if not isinstance(filters, list):
        raise ExchangeRuleError("Symbol filters are unavailable")
    mapped: dict[str, dict[str, Any]] = {}
    for item in filters:
        if not isinstance(item, dict):
            continue
        filter_type = item.get("filterType")
        if isinstance(filter_type, str):
            mapped[filter_type] = item
    return mapped


@dataclass(frozen=True)
class SymbolTradingRules:
    """Validated order filters for one Binance USD-M perpetual symbol."""

    symbol: str
    quantity_min: Decimal
    quantity_max: Decimal
    quantity_step: Decimal
    price_min: Decimal
    price_max: Decimal
    price_tick: Decimal
    min_notional: Decimal

    def normalize_market_quantity(self, raw_quantity: Decimal) -> Decimal:
        """Round Risk-approved quantity down without increasing account exposure."""

        if raw_quantity < self.quantity_min:
            raise ExchangeRuleError("Risk-approved quantity is below the exchange minimum")
        normalized = (raw_quantity / self.quantity_step).to_integral_value(
            rounding=ROUND_DOWN
        ) * self.quantity_step
        if normalized < self.quantity_min or normalized > self.quantity_max:
            raise ExchangeRuleError("Risk-approved quantity is outside exchange limits")
        if normalized % self.quantity_step != 0:
            raise ExchangeRuleError("Normalized quantity violates exchange step size")
        return normalized

    def normalize_protective_price(
        self,
        raw_price: Decimal,
        *,
        direction: ScannerDirection,
        is_stop_loss: bool,
    ) -> Decimal:
        """Normalize a protective trigger away from the entry side of the position."""

        if raw_price <= 0:
            raise ExchangeRuleError("Protective trigger price must be positive")
        round_up = (
            direction is ScannerDirection.SHORT
            if is_stop_loss
            else direction is ScannerDirection.LONG
        )
        rounding = ROUND_UP if round_up else ROUND_DOWN
        normalized = (raw_price / self.price_tick).to_integral_value(
            rounding=rounding
        ) * self.price_tick
        if normalized < self.price_min or normalized > self.price_max:
            raise ExchangeRuleError("Protective trigger price is outside exchange limits")
        if normalized % self.price_tick != 0:
            raise ExchangeRuleError("Protective trigger price violates exchange tick size")
        return normalized

    def validate_market_notional(self, *, quantity: Decimal, mark_price: Decimal) -> Decimal:
        """Validate the current mark-price notional for a market entry."""

        if mark_price <= 0:
            raise ExchangeRuleError("Exchange mark price is unavailable")
        notional = quantity * mark_price
        if notional < self.min_notional:
            raise ExchangeRuleError("Order notional is below the exchange minimum")
        return notional


def parse_symbol_trading_rules(
    exchange_info: dict[str, Any],
    *,
    symbol: str,
) -> SymbolTradingRules:
    """Parse current Binance exchangeInfo rules for one eligible USDT perpetual."""

    symbols = exchange_info.get("symbols")
    if not isinstance(symbols, list):
        raise ExchangeRuleError("Exchange symbol metadata is unavailable")
    payload = next(
        (item for item in symbols if isinstance(item, dict) and item.get("symbol") == symbol),
        None,
    )
    if payload is None:
        raise ExchangeRuleError("Symbol is not present in exchange metadata")
    if payload.get("status") != "TRADING":
        raise ExchangeRuleError("Symbol is not currently trading")
    if payload.get("contractType") != "PERPETUAL":
        raise ExchangeRuleError("Symbol is not a perpetual contract")
    if payload.get("quoteAsset") != "USDT":
        raise ExchangeRuleError("Symbol is not USDT quoted")

    filters = _filter_map(payload)
    quantity_filter = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE")
    price_filter = filters.get("PRICE_FILTER")
    notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL")
    if quantity_filter is None or price_filter is None or notional_filter is None:
        raise ExchangeRuleError("Required exchange filters are unavailable")

    quantity_min = _decimal(quantity_filter.get("minQty"), field="minQty")
    quantity_max = _decimal(quantity_filter.get("maxQty"), field="maxQty")
    quantity_step = _decimal(quantity_filter.get("stepSize"), field="stepSize")
    price_min = _decimal(price_filter.get("minPrice"), field="minPrice")
    price_max = _decimal(price_filter.get("maxPrice"), field="maxPrice")
    price_tick = _decimal(price_filter.get("tickSize"), field="tickSize")
    min_notional = _decimal(
        notional_filter.get("notional", notional_filter.get("minNotional")),
        field="minNotional",
    )

    if quantity_min <= 0 or quantity_max < quantity_min or quantity_step <= 0:
        raise ExchangeRuleError("Invalid market quantity filter")
    if price_min < 0 or price_max <= price_min or price_tick <= 0:
        raise ExchangeRuleError("Invalid price filter")
    if min_notional <= 0:
        raise ExchangeRuleError("Invalid minimum notional filter")

    return SymbolTradingRules(
        symbol=symbol,
        quantity_min=quantity_min,
        quantity_max=quantity_max,
        quantity_step=quantity_step,
        price_min=price_min,
        price_max=price_max,
        price_tick=price_tick,
        min_notional=min_notional,
    )
