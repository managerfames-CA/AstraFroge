"""Verify actual Binance Demo commission and funding economics for Journal records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.schemas.execution import DemoTradeRecord
from app.services.journal_exchange_verification import (
    JournalExchangeEvidence,
    JournalSourceVerificationError,
)

_ALLOWED_INCOME_TYPES = frozenset({"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"})
_PNL_PARITY_TOLERANCE = Decimal("0.00000001")


class JournalCostClient(Protocol):
    """Read-only Binance Demo income surface required for actual trade costs."""

    def income_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class JournalActualCostEvidence:
    """Verified actual commission, funding and net realized PnL for one trade."""

    checked_at: datetime
    income_transaction_ids: tuple[str, ...]
    realized_pnl_transaction_ids: tuple[str, ...]
    commission_transaction_ids: tuple[str, ...]
    funding_transaction_ids: tuple[str, ...]
    realized_pnl_income_usdt: Decimal
    commission_usdt: Decimal
    funding_usdt: Decimal
    net_realized_pnl_usdt: Decimal


class JournalCostVerificationService:
    """Attribute actual exchange income records to one verified closed trade."""

    def __init__(
        self,
        client: JournalCostClient | None,
        *,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._now = now_provider or (lambda: datetime.now(UTC))

    def verify(
        self,
        trade: DemoTradeRecord,
        source: JournalExchangeEvidence,
    ) -> JournalActualCostEvidence:
        """Return actual costs or reject incomplete/ambiguous income evidence."""

        if trade.closed_at is None:
            raise JournalSourceVerificationError("JOURNAL_TRADE_NOT_CLOSED")
        client = self._client
        if client is None:
            raise JournalSourceVerificationError("DEMO_PRIVATE_API_NOT_CONFIGURED")

        start_ms = int(
            (trade.opened_at - timedelta(minutes=5)).timestamp() * 1000
        )
        end_ms = int(
            (trade.closed_at + timedelta(minutes=5)).timestamp() * 1000
        )
        payloads = client.income_history(
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            limit=1000,
        )
        if len(payloads) >= 1000:
            raise JournalSourceVerificationError("JOURNAL_INCOME_WINDOW_TRUNCATED")

        all_fill_ids = frozenset((*source.entry_fill_ids, *source.close_fill_ids))
        close_fill_ids = frozenset(source.close_fill_ids)
        transaction_ids: list[str] = []
        realized_ids: list[str] = []
        commission_ids: list[str] = []
        funding_ids: list[str] = []
        realized_income = Decimal("0")
        commission = Decimal("0")
        funding = Decimal("0")

        for payload in payloads:
            if self._text(payload.get("symbol")) != trade.symbol:
                continue
            income_type = self._text(payload.get("incomeType"))
            if income_type not in _ALLOWED_INCOME_TYPES:
                continue
            if not self._inside_window(payload.get("time"), start_ms, end_ms):
                continue

            trade_id = self._text(payload.get("tradeId"))
            if income_type == "COMMISSION" and trade_id is not None:
                if trade_id not in all_fill_ids:
                    continue
            if income_type == "REALIZED_PNL" and trade_id is not None:
                if trade_id not in close_fill_ids:
                    continue

            asset = self._text(payload.get("asset"))
            if asset is not None and asset != "USDT":
                raise JournalSourceVerificationError(
                    "JOURNAL_COST_ASSET_UNSUPPORTED"
                )
            transaction_id = self._text(payload.get("tranId"))
            if transaction_id is None or transaction_id in transaction_ids:
                raise JournalSourceVerificationError(
                    "JOURNAL_INCOME_IDENTITY_INVALID"
                )
            amount = self._finite_decimal(payload.get("income"))
            transaction_ids.append(transaction_id)

            if income_type == "REALIZED_PNL":
                realized_ids.append(transaction_id)
                realized_income += amount
            elif income_type == "COMMISSION":
                commission_ids.append(transaction_id)
                commission += amount
            else:
                funding_ids.append(transaction_id)
                funding += amount

        if not realized_ids:
            raise JournalSourceVerificationError(
                "JOURNAL_REALIZED_PNL_INCOME_MISSING"
            )
        if not commission_ids:
            raise JournalSourceVerificationError("JOURNAL_COMMISSION_INCOME_MISSING")
        if commission > 0:
            raise JournalSourceVerificationError("JOURNAL_COMMISSION_SIGN_INVALID")
        if abs(realized_income - source.gross_realized_pnl_usdt) > _PNL_PARITY_TOLERANCE:
            raise JournalSourceVerificationError(
                "JOURNAL_REALIZED_PNL_INCOME_MISMATCH"
            )

        return JournalActualCostEvidence(
            checked_at=self._now(),
            income_transaction_ids=tuple(sorted(transaction_ids)),
            realized_pnl_transaction_ids=tuple(sorted(realized_ids)),
            commission_transaction_ids=tuple(sorted(commission_ids)),
            funding_transaction_ids=tuple(sorted(funding_ids)),
            realized_pnl_income_usdt=realized_income,
            commission_usdt=commission,
            funding_usdt=funding,
            net_realized_pnl_usdt=(
                source.gross_realized_pnl_usdt + commission + funding
            ),
        )

    @staticmethod
    def _inside_window(value: Any, start_ms: int, end_ms: int) -> bool:
        if value is None:
            return True
        try:
            timestamp_ms = int(value)
        except (TypeError, ValueError) as exc:
            raise JournalSourceVerificationError(
                "JOURNAL_INCOME_TIMESTAMP_INVALID"
            ) from exc
        return start_ms <= timestamp_ms <= end_ms

    @staticmethod
    def _text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _finite_decimal(value: Any) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise JournalSourceVerificationError(
                "JOURNAL_EXCHANGE_DECIMAL_INVALID"
            ) from exc
        if not parsed.is_finite():
            raise JournalSourceVerificationError(
                "JOURNAL_EXCHANGE_DECIMAL_INVALID"
            )
        return parsed
