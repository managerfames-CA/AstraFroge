"""Read-only verification of exchange sources required for Journal records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.schemas.execution import (
    DemoTradeCloseReason,
    DemoTradeLifecycle,
    DemoTradeRecord,
)
from app.schemas.scanner import ScannerDirection

_REGULAR_FILLED_STATUSES = frozenset({"FILLED"})
_ALGO_TERMINAL_STATUSES = frozenset({"FILLED", "FINISHED"})


class JournalExchangeClient(Protocol):
    """Binance Demo read surface required to prove Journal source integrity."""

    def query_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]: ...

    def query_algo_order(
        self,
        *,
        symbol: str,
        orig_client_order_id: str,
    ) -> dict[str, Any]: ...

    def user_trades(
        self,
        *,
        symbol: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]: ...

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]: ...


class JournalSourceVerificationError(RuntimeError):
    """Stable secret-safe reason why a candidate Journal record was rejected."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class VerifiedFillEconomics:
    """One order's exchange-verified aggregate fill economics."""

    fill_ids: tuple[str, ...]
    quantity: Decimal
    notional: Decimal
    average_price: Decimal


@dataclass(frozen=True)
class JournalExchangeEvidence:
    """Verified exchange identities and fill-derived economics for one Journal record."""

    checked_at: datetime
    entry_exchange_order_id: str
    close_exchange_order_id: str
    entry_fill_ids: tuple[str, ...]
    close_fill_ids: tuple[str, ...]
    income_transaction_ids: tuple[str, ...]
    entry_fill_quantity: Decimal
    close_fill_quantity: Decimal
    entry_average_price: Decimal
    close_average_price: Decimal
    gross_realized_pnl_usdt: Decimal


class JournalExchangeVerificationService:
    """Prove order, fill and income sources before a closed trade enters Journal."""

    def __init__(
        self,
        client: JournalExchangeClient | None,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._now = now_provider or (lambda: datetime.now(UTC))

    def verify(self, trade: DemoTradeRecord) -> JournalExchangeEvidence:
        """Return verified source evidence or reject the candidate fail closed."""

        if trade.lifecycle is not DemoTradeLifecycle.CLOSED or trade.closed_at is None:
            raise JournalSourceVerificationError("JOURNAL_TRADE_NOT_CLOSED")
        client = self._client
        if client is None:
            raise JournalSourceVerificationError("DEMO_PRIVATE_API_NOT_CONFIGURED")

        entry = client.query_order(
            symbol=trade.symbol,
            orig_client_order_id=trade.client_order_id,
        )
        entry_order_id = self._verified_order(
            entry,
            expected_client_order_id=trade.client_order_id,
            expected_exchange_order_id=trade.exchange_order_id,
            expected_quantity=trade.executed_quantity,
            accepted_statuses=_REGULAR_FILLED_STATUSES,
            code_prefix="ENTRY",
        )

        close_client_order_id, algo = self._close_identity(trade)
        close = (
            client.query_algo_order(
                symbol=trade.symbol,
                orig_client_order_id=close_client_order_id,
            )
            if algo
            else client.query_order(
                symbol=trade.symbol,
                orig_client_order_id=close_client_order_id,
            )
        )
        close_order_id = self._verified_order(
            close,
            expected_client_order_id=close_client_order_id,
            expected_exchange_order_id=None,
            expected_quantity=None if algo else trade.executed_quantity,
            accepted_statuses=(_ALGO_TERMINAL_STATUSES if algo else _REGULAR_FILLED_STATUSES),
            code_prefix="CLOSE",
        )

        start_ms = int((trade.opened_at - timedelta(minutes=5)).timestamp() * 1000)
        end_ms = int((trade.closed_at + timedelta(minutes=5)).timestamp() * 1000)
        fills = client.user_trades(
            symbol=trade.symbol,
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            limit=1000,
        )
        if len(fills) >= 1000:
            raise JournalSourceVerificationError("JOURNAL_FILL_WINDOW_TRUNCATED")
        entry_fills = self._verified_fills(
            fills,
            symbol=trade.symbol,
            order_id=entry_order_id,
            expected_quantity=trade.executed_quantity,
            code_prefix="ENTRY",
        )
        close_fills = self._verified_fills(
            fills,
            symbol=trade.symbol,
            order_id=close_order_id,
            expected_quantity=trade.executed_quantity,
            code_prefix="CLOSE",
        )

        income = client.income_history(
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            limit=1000,
        )
        if len(income) >= 1000:
            raise JournalSourceVerificationError("JOURNAL_INCOME_WINDOW_TRUNCATED")
        income_ids = self._verified_income(income, symbol=trade.symbol)

        gross_realized_pnl = self._gross_realized_pnl(
            direction=trade.direction,
            entry_average_price=entry_fills.average_price,
            close_average_price=close_fills.average_price,
            quantity=entry_fills.quantity,
        )
        return JournalExchangeEvidence(
            checked_at=self._now(),
            entry_exchange_order_id=entry_order_id,
            close_exchange_order_id=close_order_id,
            entry_fill_ids=entry_fills.fill_ids,
            close_fill_ids=close_fills.fill_ids,
            income_transaction_ids=income_ids,
            entry_fill_quantity=entry_fills.quantity,
            close_fill_quantity=close_fills.quantity,
            entry_average_price=entry_fills.average_price,
            close_average_price=close_fills.average_price,
            gross_realized_pnl_usdt=gross_realized_pnl,
        )

    @staticmethod
    def _close_identity(trade: DemoTradeRecord) -> tuple[str, bool]:
        if trade.closed_reason is DemoTradeCloseReason.STOP_LOSS:
            return trade.stop_client_order_id, True
        if trade.closed_reason is DemoTradeCloseReason.TAKE_PROFIT:
            return trade.take_profit_client_order_id, True
        if trade.closed_reason in {
            DemoTradeCloseReason.MANUAL_CLOSE,
            DemoTradeCloseReason.INVALIDATED,
        }:
            return f"af-m-{trade.signal_id[:20]}", False
        raise JournalSourceVerificationError("JOURNAL_CLOSE_REASON_UNVERIFIED")

    @classmethod
    def _verified_order(
        cls,
        payload: dict[str, Any],
        *,
        expected_client_order_id: str,
        expected_exchange_order_id: str | None,
        expected_quantity: Decimal | None,
        accepted_statuses: frozenset[str],
        code_prefix: str,
    ) -> str:
        client_id = cls._text(payload.get("clientOrderId"))
        exchange_id = cls._text(payload.get("actualOrderId")) or cls._text(payload.get("orderId"))
        status = cls._text(payload.get("status"))
        if client_id != expected_client_order_id or exchange_id is None:
            raise JournalSourceVerificationError(f"JOURNAL_{code_prefix}_ORDER_IDENTITY_INVALID")
        if expected_exchange_order_id is not None and exchange_id != expected_exchange_order_id:
            raise JournalSourceVerificationError(f"JOURNAL_{code_prefix}_ORDER_IDENTITY_INVALID")
        if status not in accepted_statuses:
            raise JournalSourceVerificationError(f"JOURNAL_{code_prefix}_ORDER_NOT_FILLED")
        if expected_quantity is not None:
            quantity = cls._positive_decimal(payload.get("executedQty"))
            if quantity != expected_quantity:
                raise JournalSourceVerificationError(
                    f"JOURNAL_{code_prefix}_ORDER_QUANTITY_MISMATCH"
                )
        return exchange_id

    @classmethod
    def _verified_fills(
        cls,
        payloads: list[dict[str, Any]],
        *,
        symbol: str,
        order_id: str,
        expected_quantity: Decimal,
        code_prefix: str,
    ) -> VerifiedFillEconomics:
        fill_ids: list[str] = []
        quantity = Decimal("0")
        notional = Decimal("0")
        for payload in payloads:
            if cls._text(payload.get("symbol")) != symbol:
                continue
            if cls._text(payload.get("orderId")) != order_id:
                continue
            fill_id = cls._text(payload.get("id"))
            if fill_id is None or fill_id in fill_ids:
                raise JournalSourceVerificationError(f"JOURNAL_{code_prefix}_FILL_IDENTITY_INVALID")
            fill_quantity = cls._positive_decimal(payload.get("qty"))
            fill_price = cls._positive_decimal(payload.get("price"))
            fill_ids.append(fill_id)
            quantity += fill_quantity
            notional += fill_quantity * fill_price
        if not fill_ids:
            raise JournalSourceVerificationError(f"JOURNAL_{code_prefix}_FILL_MISSING")
        if quantity != expected_quantity:
            raise JournalSourceVerificationError(f"JOURNAL_{code_prefix}_FILL_QUANTITY_MISMATCH")
        if notional <= 0:
            raise JournalSourceVerificationError(f"JOURNAL_{code_prefix}_FILL_NOTIONAL_INVALID")
        return VerifiedFillEconomics(
            fill_ids=tuple(sorted(fill_ids)),
            quantity=quantity,
            notional=notional,
            average_price=notional / quantity,
        )

    @staticmethod
    def _gross_realized_pnl(
        *,
        direction: ScannerDirection,
        entry_average_price: Decimal,
        close_average_price: Decimal,
        quantity: Decimal,
    ) -> Decimal:
        price_delta = (
            close_average_price - entry_average_price
            if direction is ScannerDirection.LONG
            else entry_average_price - close_average_price
        )
        return price_delta * quantity

    @classmethod
    def _verified_income(
        cls,
        payloads: list[dict[str, Any]],
        *,
        symbol: str,
    ) -> tuple[str, ...]:
        transaction_ids: list[str] = []
        realized_pnl_found = False
        for payload in payloads:
            if cls._text(payload.get("symbol")) != symbol:
                continue
            income_type = cls._text(payload.get("incomeType"))
            if income_type not in {
                "REALIZED_PNL",
                "COMMISSION",
                "FUNDING_FEE",
            }:
                continue
            transaction_id = cls._text(payload.get("tranId"))
            if transaction_id is None or transaction_id in transaction_ids:
                raise JournalSourceVerificationError("JOURNAL_INCOME_IDENTITY_INVALID")
            _ = cls._finite_decimal(payload.get("income"))
            if income_type == "REALIZED_PNL":
                realized_pnl_found = True
            transaction_ids.append(transaction_id)
        if not realized_pnl_found:
            raise JournalSourceVerificationError("JOURNAL_REALIZED_PNL_INCOME_MISSING")
        return tuple(sorted(transaction_ids))

    @staticmethod
    def _text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _positive_decimal(cls, value: Any) -> Decimal:
        parsed = cls._finite_decimal(value)
        if parsed <= 0:
            raise JournalSourceVerificationError("JOURNAL_EXCHANGE_DECIMAL_INVALID")
        return parsed

    @staticmethod
    def _finite_decimal(value: Any) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise JournalSourceVerificationError("JOURNAL_EXCHANGE_DECIMAL_INVALID") from exc
        if not parsed.is_finite():
            raise JournalSourceVerificationError("JOURNAL_EXCHANGE_DECIMAL_INVALID")
        return parsed
