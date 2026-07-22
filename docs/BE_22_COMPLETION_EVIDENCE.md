# BE-22 Completion Evidence: Pytest Coverage and Suite Execution Verification

## Required Coverage Threshold Discovery

Upon inspecting the repository's configuration in `pyproject.toml`, the following strict coverage requirements were verified:

```toml
[tool.coverage.report]
show_missing = true
skip_covered = true
fail_under = 90
```

This specifies a required coverage threshold of **90%** across the entire application (`app/`).

---

## Pytest Execution Results

The full pytest suite was executed using:
```bash
poetry run pytest --cov=app --cov-fail-under=90
```

All **693 tests passed successfully**, achieving a total code coverage of **90.47%**, safely above the required **90%** threshold.

### Coverage Gaps Closed

In order to ensure maximum safety on critical fail-closed execution paths, extra unit tests were added to `tests/unit/test_execution_leader_safety.py`. These targeted tests covered:
- Valid connection reuse in `ValidatedExecutionLeaderLease.acquire`.
- Handling of invalid connection reacquisition during lease acquisition.
- Unacquired and closed lease validation errors in `require_valid`.
- Simulation of database query exceptions and ownership loss validations.
- Suppressed connection-close exceptions inside `_discard_lost_connection`.
- Bypassed validations when recovery is not required.
- Integration validation inside `status()` and `plans()`.
- Successful automation loop paths and generic/specific exception processing inside `auto_execute_pending()`.
- Delegation of custom service attributes and fallback to `__getattr__`.

As a result, the critical fail-closed advisory lock module `app/services/execution_leader_safety.py` achieved **100% complete coverage** (and was successfully skipped in the partial coverage report due to full coverage).

---

## Final Coverage Report Summary

```text
Name                                               Stmts   Miss Branch BrPart  Cover   Missing
----------------------------------------------------------------------------------------------
app/api/v1/active_trade_dependencies.py                4      1      0      0    75%   13
app/api/v1/dependencies.py                           172     14     20      6    90%   128-130, 146, 157, 176-177, 309, 330, 343, 350, 357-358, 375->377, 379
app/api/v1/notification_dependencies.py                8      4      2      0    40%   12-15
app/api/v1/routes/execution.py                        83      1     16      4    95%   94->96, 96->98, 98->100, 249
app/api/v1/routes/global_reconciliation.py            11      1      2      1    85%   20
app/api/v1/routes/health.py                           22      2      4      2    85%   17, 19
app/api/v1/routes/journal_performance.py              27      2      2      0    93%   77-78
app/api/v1/routes/lifecycle_reconciliation.py         16      2      4      2    80%   31, 36
app/api/v1/routes/market.py                           42     19      4      0    54%   21-24, 31-34, 49-50, 60-68
app/api/v1/routes/notifications.py                    30     14      4      0    47%   27-29, 44-51, 59-62, 73-76
app/api/v1/routes/order_audit.py                      35      1     10      2    93%   32->36, 38
app/api/v1/routes/order_reconciliation.py             11      4      2      0    54%   18-21
app/api/v1/routes/position_reconciliation.py          11      4      2      0    54%   22-28
app/api/v1/routes/protective_lifecycle.py             11      1      2      1    85%   20
app/api/v1/routes/restart_recovery.py                 11      1      2      1    85%   22
app/api/v1/routes/scanner.py                          75     15     18      2    75%   36-42, 59-73, 88
app/core/config.py                                   163     14     60      9    87%   69, 71, 73-78, 95, 122, 139, 165, 205, 211
app/core/errors.py                                    40      1      0      0    98%   119
app/core/logging.py                                   65      3     24      5    91%   49->48, 65, 67, 83, 84->86
app/core/security.py                                 136      8     46      8    91%   63, 77, 79, 140, 142, 161, 278, 297
app/integrations/binance/pooled_clients.py            81      3     16      2    95%   43, 49-50
app/integrations/binance/private_demo_client.py      109     12     18      8    84%   82, 88-89, 90->92, 93->95, 103-104, 111, 119->121, 121->123, 143, 157, 179, 258, 286-287
app/integrations/binance/public_client.py            100      4     28      4    94%   53, 79, 124, 151
app/integrations/binance/recovery_demo_client.py      35      2     10      4    87%   27->29, 40, 50, 53->55
app/main.py                                          169     42     46     10    70%   73, 78, 81, 84, 87, 97->85, 116-132, 135-148, 166-175, 223-236, 242-245, 249
app/persistence/database.py                           70      2     20      2    96%   61, 65
app/persistence/execution_command_repository.py      167     12     36      6    90%   155-158, 231, 282, 355, 490-494, 501-503
app/persistence/repositories.py                      124     19     30     10    80%   31, 38, 49, 200->203, 311->313, 328, 351, 368-381, 395-401, 411-413, 445-457
app/persistence/service_adapters.py                  207      5     44      5    96%   118->120, 256, 260->276, 423, 437, 445-446
app/schemas/scanner.py                               261      2     14      2    99%   168, 205
app/services/account_snapshot.py                     257     21     46     10    89%   209->207, 211-212, 213->207, 271, 274, 288, 308, 368-370, 375-376, 445, 533, 546, 549, 552, 555, 567, 570, 573, 576
app/services/active_trade_authority.py               105     13     38      7    86%   63->65, 108, 128-129, 174, 182-183, 185-186, 263, 269, 277-279
app/services/decision_signals.py                     146     10     38      3    92%   108->121, 114, 166-174, 303-305, 314-321
app/services/durable_trade_management.py              95     25     24      7    71%   41, 64-65, 77-92, 100, 114-115, 128-129, 136-137, 183-184, 193->202, 200-201, 212, 226
app/services/exchange_rules.py                        96      3     44      4    95%   33, 35->31, 64, 90
app/services/execution.py                            357     64    112     34    79%   141-142, 196-197, 206-207, 212, 215, 222, 236, 288, 294, 300, 306, 312, 345, 351, 363-364, 377->384, 395, 445->451, 520-525, 529-533, 540-545, 558-559, 579-581, 589-598, 640-641, 651-652, 654, 667, 673, 693, 714, 734, 751, 753, 770, 794->797, 821, 833, 839, 845, 853, 874, 880, 886, 893-894, 900, 917
app/services/execution_command.py                    223     35     96     21    81%   67, 148-160, 171, 173, 175, 180, 182, 184, 186, 188, 190, 192, 199, 202, 285, 288-289, 294, 298, 305, 359, 361, 363, 385->384
app/services/execution_private_adapter.py             24      1      6      1    93%   49
app/services/execution_worker.py                     129      3     30      0    98%   235-237
app/services/global_reconciliation.py                106      5     38      5    93%   90->93, 94-95, 97, 124, 126
app/services/indicators.py                           147      3     34      1    98%   100, 251-252
app/services/journal_cost_verification.py            110     11     44      9    87%   68, 71, 81, 95, 98, 100, 108, 130, 171-172, 174
app/services/journal_exchange_verification.py        157      7     56      5    94%   231, 257, 273, 307, 314, 344-345
app/services/journal_performance.py                  139     12     28      5    90%   128, 137, 275-280, 292-293, 333, 350
app/services/lifecycle_reconciliation.py              43      0     14      1    98%   75->85
app/services/manual_close_durability.py              176     30     40     15    78%   90-98, 112, 119, 135, 138, 165, 167, 205, 215, 251, 254, 267, 276-277, 297-298, 307, 313-314, 316, 333, 336-337, 339
app/services/market_data.py                          115     11     42     11    86%   63, 66, 76, 81, 84, 107, 131, 150, 154, 161, 168
app/services/notifications.py                        106     11     38      5    88%   94-103, 107, 185, 193, 232, 235
app/services/order_audit.py                          444     18    142     15    94%   132, 166-168, 561, 637, 771, 805, 810, 815, 820, 826, 831, 836, 841, 846, 856, 861
app/services/order_audit_runtime.py                   93     11     58      2    85%   169, 187-212
app/services/order_reconciliation.py                 147     29     54     13    77%   78-83, 105-113, 166-167, 175, 197, 206, 224, 245-253, 255, 264, 292-301, 310, 330, 342, 379, 387-388, 390
app/services/performance_reporting.py                 65      2     18      2    95%   34, 110
app/services/position_reconciliation.py              117      8     36      2    91%   73-78, 282, 293
app/services/protective_lifecycle.py                 418     23    122     19    92%   248-249, 303, 313-314, 327, 455, 460, 476, 482, 490, 578, 671->677, 688, 694, 718, 748, 754, 785, 787, 827, 850, 852, 903
app/services/recovery.py                             304      9    108      7    96%   142, 353-354, 384, 397, 399, 401, 408, 463
app/services/restart_recovery.py                      99      1     36      1    99%   202
app/services/risk.py                                 234     12     88      9    93%   340-341, 352, 356, 397, 401, 416, 433, 478, 481-482, 491
app/services/scanner.py                              156     28     50     13    79%   41, 61, 69-77, 104, 144, 182, 189, 213-230, 232-239, 251-262, 276-277, 332, 359->361, 364, 383->385
app/services/scanner_base.py                         239     16     76     16    90%   87, 97, 107, 134, 240, 252, 264, 271, 302, 306, 313, 334, 346, 388, 395, 411
app/services/scanner_full.py                         176     19     54      2    90%   276-278, 288-307, 404->416, 414->416
app/services/scanner_opportunity.py                   90      7     36     16    82%   57, 89, 93, 99, 139, 159, 207->214, 212->214, 218->exit, 219->221, 221->224, 222->224, 224->exit, 225->exit, 241->240, 245
app/services/scanner_runtime.py                      289     16     78      7    93%   129, 131-133, 138-141, 156-158, 216->224, 418->427, 428, 452, 456-457, 487-488, 525->exit
app/services/scanner_scoring.py                       74      5     22      6    89%   223->225, 242, 249, 272, 295, 299
app/services/scanner_setups.py                       177     12     58     12    90%   79, 98, 166, 170, 176, 216, 244, 293, 313, 358, 463, 467
app/services/scanner_strategy_separated.py           125      0     36      5    97%   56->58, 58->44, 107->109, 109->111, 220->222
app/services/scanner_universe.py                      58      3      4      1    94%   65, 84-85
app/services/shared_snapshots.py                     202     14     40      8    91%   81, 136, 139-140, 142, 211, 213, 254, 261, 354-355, 365-367
app/services/signal_decision.py                      130     12     70     10    89%   78, 84, 86, 89-90, 126, 195, 197, 199, 201, 203, 230
app/services/signals.py                              134      2     38      4    97%   168, 225->228, 276, 287->289
app/services/trade_management.py                     175     28     58     14    81%   112, 119, 149, 156, 175-176, 232->236, 252, 262-271, 284, 290, 315-316, 322, 375, 378, 381, 392, 413-414, 416, 431-432
app/services/universe.py                             146      2     52      3    97%   43, 51->49, 82
----------------------------------------------------------------------------------------------
TOTAL                                              10667    757   2662    427    90%

48 files skipped due to complete coverage.
Required test coverage of 90% reached. Total coverage: 90.47%
====================== 693 passed, 99 warnings in 54.52s =======================
```

All quality gates pass. Both linting (`ruff`) and strict typing (`mypy`) continue to pass alongside the fully validated test coverage.
