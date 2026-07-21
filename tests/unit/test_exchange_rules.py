"""Exact Binance exchange-rule parsing and normalization tests."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from app.schemas.scanner import ScannerDirection
from app.services.exchange_rules import ExchangeRuleError, parse_symbol_trading_rules


def _exchange_info() -> dict[str, object]:
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.10",
                        "maxPrice": "1000000",
                        "tickSize": "0.10",
                    },
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.001",
                        "maxQty": "1000",
                        "stepSize": "0.001",
                    },
                    {
                        "filterType": "MARKET_LOT_SIZE",
                        "minQty": "0.005",
                        "maxQty": "100",
                        "stepSize": "0.005",
                    },
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }
        ]
    }


def _symbol(payload: dict[str, object]) -> dict[str, object]:
    symbols = payload["symbols"]
    assert isinstance(symbols, list)
    symbol = symbols[0]
    assert isinstance(symbol, dict)
    return symbol


def test_parses_market_specific_quantity_rules_and_rounds_down() -> None:
    rules = parse_symbol_trading_rules(_exchange_info(), symbol="BTCUSDT")

    assert rules.quantity_min == Decimal("0.005")
    assert rules.quantity_step == Decimal("0.005")
    assert rules.normalize_market_quantity(Decimal("0.1078")) == Decimal("0.105")


def test_protective_prices_round_away_from_position_entry_side() -> None:
    rules = parse_symbol_trading_rules(_exchange_info(), symbol="BTCUSDT")

    assert rules.normalize_protective_price(
        Decimal("95.06"),
        direction=ScannerDirection.LONG,
        is_stop_loss=True,
    ) == Decimal("95.0")
    assert rules.normalize_protective_price(
        Decimal("114.54"),
        direction=ScannerDirection.LONG,
        is_stop_loss=False,
    ) == Decimal("114.6")
    assert rules.normalize_protective_price(
        Decimal("105.04"),
        direction=ScannerDirection.SHORT,
        is_stop_loss=True,
    ) == Decimal("105.1")
    assert rules.normalize_protective_price(
        Decimal("89.96"),
        direction=ScannerDirection.SHORT,
        is_stop_loss=False,
    ) == Decimal("89.9")


def test_rejects_below_minimum_quantity_and_notional() -> None:
    rules = parse_symbol_trading_rules(_exchange_info(), symbol="BTCUSDT")

    with pytest.raises(ExchangeRuleError, match="below the exchange minimum"):
        rules.normalize_market_quantity(Decimal("0.004"))
    with pytest.raises(ExchangeRuleError, match="notional"):
        rules.validate_market_notional(
            quantity=Decimal("0.005"),
            mark_price=Decimal("100"),
        )


def test_rejects_quantity_above_maximum_and_invalid_mark_price() -> None:
    rules = parse_symbol_trading_rules(_exchange_info(), symbol="BTCUSDT")

    with pytest.raises(ExchangeRuleError, match="outside exchange limits"):
        rules.normalize_market_quantity(Decimal("100.005"))
    with pytest.raises(ExchangeRuleError, match="mark price"):
        rules.validate_market_notional(quantity=Decimal("1"), mark_price=Decimal("0"))


def test_rejects_invalid_or_out_of_range_protective_prices() -> None:
    rules = parse_symbol_trading_rules(_exchange_info(), symbol="BTCUSDT")

    with pytest.raises(ExchangeRuleError, match="positive"):
        rules.normalize_protective_price(
            Decimal("0"),
            direction=ScannerDirection.LONG,
            is_stop_loss=True,
        )
    with pytest.raises(ExchangeRuleError, match="outside exchange limits"):
        rules.normalize_protective_price(
            Decimal("1000001"),
            direction=ScannerDirection.SHORT,
            is_stop_loss=True,
        )


def test_rejects_ineligible_or_malformed_symbol_metadata() -> None:
    payload = _exchange_info()
    _symbol(payload)["status"] = "BREAK"
    with pytest.raises(ExchangeRuleError, match="not currently trading"):
        parse_symbol_trading_rules(payload, symbol="BTCUSDT")

    with pytest.raises(ExchangeRuleError, match="metadata"):
        parse_symbol_trading_rules({"symbols": "bad"}, symbol="BTCUSDT")
    with pytest.raises(ExchangeRuleError, match="not present"):
        parse_symbol_trading_rules(_exchange_info(), symbol="ETHUSDT")


def test_rejects_wrong_contract_and_quote_asset() -> None:
    contract_payload = _exchange_info()
    _symbol(contract_payload)["contractType"] = "CURRENT_QUARTER"
    with pytest.raises(ExchangeRuleError, match="perpetual"):
        parse_symbol_trading_rules(contract_payload, symbol="BTCUSDT")

    quote_payload = _exchange_info()
    _symbol(quote_payload)["quoteAsset"] = "BUSD"
    with pytest.raises(ExchangeRuleError, match="USDT quoted"):
        parse_symbol_trading_rules(quote_payload, symbol="BTCUSDT")


def test_rejects_missing_or_malformed_filters() -> None:
    missing_payload = _exchange_info()
    _symbol(missing_payload)["filters"] = []
    with pytest.raises(ExchangeRuleError, match="Required exchange filters"):
        parse_symbol_trading_rules(missing_payload, symbol="BTCUSDT")

    malformed_payload = _exchange_info()
    _symbol(malformed_payload)["filters"] = "bad"
    with pytest.raises(ExchangeRuleError, match="filters are unavailable"):
        parse_symbol_trading_rules(malformed_payload, symbol="BTCUSDT")


def test_rejects_invalid_decimal_and_non_finite_filter_values() -> None:
    invalid_payload = deepcopy(_exchange_info())
    filters = _symbol(invalid_payload)["filters"]
    assert isinstance(filters, list)
    assert isinstance(filters[0], dict)
    filters[0]["tickSize"] = "not-a-number"
    with pytest.raises(ExchangeRuleError, match="tickSize"):
        parse_symbol_trading_rules(invalid_payload, symbol="BTCUSDT")

    infinite_payload = deepcopy(_exchange_info())
    infinite_filters = _symbol(infinite_payload)["filters"]
    assert isinstance(infinite_filters, list)
    assert isinstance(infinite_filters[3], dict)
    infinite_filters[3]["notional"] = "Infinity"
    with pytest.raises(ExchangeRuleError, match="minNotional"):
        parse_symbol_trading_rules(infinite_payload, symbol="BTCUSDT")


def test_rejects_invalid_quantity_price_and_notional_filter_ranges() -> None:
    quantity_payload = deepcopy(_exchange_info())
    quantity_filters = _symbol(quantity_payload)["filters"]
    assert isinstance(quantity_filters, list)
    assert isinstance(quantity_filters[2], dict)
    quantity_filters[2]["stepSize"] = "0"
    with pytest.raises(ExchangeRuleError, match="quantity filter"):
        parse_symbol_trading_rules(quantity_payload, symbol="BTCUSDT")

    price_payload = deepcopy(_exchange_info())
    price_filters = _symbol(price_payload)["filters"]
    assert isinstance(price_filters, list)
    assert isinstance(price_filters[0], dict)
    price_filters[0]["maxPrice"] = "0.10"
    with pytest.raises(ExchangeRuleError, match="price filter"):
        parse_symbol_trading_rules(price_payload, symbol="BTCUSDT")

    notional_payload = deepcopy(_exchange_info())
    notional_filters = _symbol(notional_payload)["filters"]
    assert isinstance(notional_filters, list)
    assert isinstance(notional_filters[3], dict)
    notional_filters[3]["notional"] = "0"
    with pytest.raises(ExchangeRuleError, match="minimum notional"):
        parse_symbol_trading_rules(notional_payload, symbol="BTCUSDT")
