# BE-01 Durable Persistence Audit

Date: 2026-07-17

## Existing process-scoped state

- `SignalService._records_by_candidate` and `_candidate_by_signal` hold Signal records and lifecycle history only for the current process.
- `RiskService` rebuilds assessments from current Signals and a current Binance Demo account snapshot; decisions have no durable audit record.
- `DemoExecutionService._trades` holds exchange-confirmed protected Demo trades only for the current process.
- Exchange order IDs, deterministic client IDs and verified aggregate fill values are embedded in the in-memory trade DTO, not normalized into durable order/fill records.
- Position data is read from the Binance Demo account endpoint but is not stored.
- Trade Management updates the process-scoped trade record after an exchange-authoritative close.
- Journal/Performance consumes current Trade Management records and therefore cannot recover history after restart.
- The mutation replay registry is intentionally out of BE-01 scope and remains process-scoped for BE-02.

## Authoritative and derived fields

### Authoritative

- Stable Signal ID, candidate/source identity, lifecycle transition, transition reason and UTC event time.
- Risk decision, rejection/audit codes, policy inputs captured in the assessment and UTC assessment time.
- Binance Demo client order ID, exchange order ID, order status and exchange-returned quantities/prices.
- Binance Demo fill/trade identity when supplied by the exchange, fill quantity, price, commission and UTC fill time.
- Binance Demo position quantity, entry price and snapshot time.
- Trade lifecycle, exchange-confirmed entry/exit values and UTC lifecycle times.

### Derived

- UI summaries, counts and filters.
- Plan projections from current Risk assessments.
- Gross PnL calculations and journal/performance aggregations.
- Current available slots and other status summaries.

Derived values may be included in immutable payload snapshots for audit context, but the normalized authoritative columns remain the durable source of truth.

## Selected architecture

- PostgreSQL is the staging/production database.
- SQLAlchemy 2 provides the central persistence boundary and explicit transactions.
- Alembic owns schema migration support and startup upgrades.
- Decimal financial values are stored as canonical strings to preserve exact input precision and avoid binary floating-point conversion.
- SQLite is allowed only as an isolated deterministic test backend. Staging/production reject SQLite and reject a missing database URL.
- Development may run without persistence while execution remains locked; there is no silent memory fallback in staging/production.

## Deployment constraints

- `ASTRAFORGE_DATABASE_URL` must be configured as a PostgreSQL URL in staging/production.
- Database connectivity and migration failure abort application startup.
- No API keys, bearer tokens, signatures or secrets belong in persisted payloads.
- Real trading remains disabled. The persistence layer does not enable execution or alter Scanner, Signal grading, Risk or Binance Demo semantics.

## BE-01 scope boundary

This foundation does not implement durable mutation replay, continuous reconciliation, performance analytics, notifications, paper trading, Testnet or live trading.
