# BE-16 Implementation Notes

Current clean branch scope:

- typed notification contracts
- durable notification authority
- outage-cycle-specific deduplication
- authorized mark-read mutation boundary
- durable database migration
- focused contract tests

Still required before ready-for-review:

- wire the service through application dependencies and router
- add the ORM row model
- integrate exchange-authoritative order/fill/TP/SL, Risk, connection and reconciliation triggers
- add initial-observation fill regression tests
- run Backend CI and resolve all review findings
