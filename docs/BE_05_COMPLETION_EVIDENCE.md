# BE-05 Completion Evidence

## Locked requirement

Detect partial fills, external closes, missing protective orders and exchange/runtime mismatches.

## Completion candidate

- Date/time: **2026-07-19 19:40 BDT (UTC+06:00)**
- Branch: `be-05-lifecycle-mismatch-detection`
- Pull request: pending creation
- Merge commit: pending owner merge
- CI: pending

## Implemented evidence

- `app/services/order_reconciliation.py`
  - classifies entry partial fills as `ENTRY_ORDER_PARTIAL_FILL`
  - classifies protective-order partial fills as `PROTECTIVE_ORDER_PARTIAL_FILL`
  - preserves explicit missing-protection detection as `PROTECTIVE_ORDER_MISSING`
  - separates entry identity, status and fill-quantity mismatches
- `app/services/position_reconciliation.py`
  - classifies a durable open trade with no exchange position as `EXTERNAL_POSITION_CLOSE_DETECTED`
  - retains direction, quantity, duplicate, orphan and malformed position mismatch detection
- `app/services/lifecycle_reconciliation.py`
  - normalizes continuously refreshed order and position findings into the required BE-05 categories
  - reports `PARTIAL_FILL`, `EXTERNAL_CLOSE`, `MISSING_PROTECTION` and `EXCHANGE_RUNTIME_MISMATCH`
  - remains fail-closed until both continuous reconciliation sources have proved exchange truth
- `GET /api/v1/lifecycle-reconciliation/status`
  - exposes one typed, secret-safe lifecycle mismatch view

## Verification scope

Focused regression coverage includes:

- verified in-sync lifecycle
- entry partial fill
- protective-order partial fill
- external position close
- missing protective order
- direction and quantity drift
- source-not-ready fail-closed behavior
- OpenAPI and runtime status contracts

Required merge gates:

```bash
ruff check .
mypy app
pytest -q --cov=app --cov-report=term-missing
python -c "from app.main import app; assert app.title == 'AstraForge Crypto Backend'"
docker build -t astraforge-backend:verify .
```

## Explicit exclusions

- BE-06 restart/deployment recovery is not implemented here.
- BE-08 journal construction from verified exchange records is not implemented here.
- BE-11 exchange-authoritative Active Trades remains pending.
- Live or real-money trading remains unavailable and disabled.
