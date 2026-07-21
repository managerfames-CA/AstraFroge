# BE-16 Review Checklist

- [ ] ORM model and migration agree exactly.
- [ ] Dependency factory and API router are wired.
- [ ] Mark-read mutation uses the repository mutation authorization and idempotency boundary.
- [ ] Initial authoritative filled observation emits fill and TP/SL notifications.
- [ ] Every outage/recovery cycle has a distinct deterministic identity.
- [ ] Risk, connection and reconciliation delivery failures remain separate from execution results.
- [ ] Focused tests and full Backend CI pass on the final head.
- [ ] No BE-17 or later scope is present.
