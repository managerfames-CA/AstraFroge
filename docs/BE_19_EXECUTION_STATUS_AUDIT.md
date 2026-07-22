# BE-19 EXECUTION STATUS AUDIT: TRUTHFUL INTEGRATION AND RECOVERY STATUS

## 1. Executive Summary

An audit of the AstraForge Binance USD-M Futures Demo intraday trading system execution engine was performed to investigate the relationship between execution readiness, credentials configuration, and API availability.

The audit revealed that the empty/unavailable returns from `/execution/demo/plans` and `/execution/demo/commands` under enabled execution and connected demo account conditions represent **intentional fail-closed behavior (Option A)** rather than a wiring bug. Automated execution plans and commands are generated downstream from discovered, qualified strategy signals and require a completed, verified startup recovery session.

To resolve the frontend contradiction and display consistent state, we have extended the `DemoExecutionStatusResponse` schema with two explicit fields: `execution_integration_ready: bool` and `execution_unavailable_reason: str | None`. These fields provide the frontend with the exact, human-readable status of the underlying pipeline.

## 2. Technical Design and Trace Analysis

### 2.1 Trace Analysis of Empty Plans and Commands
1. **Plans Endpoint (`/execution/demo/plans`)**:
   - Plans are mapped directly from Risk Assessments: `[self._to_plan(item) for item in self._risk.assessments().assessments]`.
   - Risk Assessments are built from discovered active signals: `self._signals.signals().signals`.
   - If the Scanner has not discovered or qualified any signals (e.g. no symbols meet the intraday technical setup criteria or the system has just started), `/plans` will be completely empty. This is the correct, fail-closed state.
2. **Commands Endpoint (`/execution/demo/commands`)**:
   - Commands are listed from the persistent execution command repository: `self._repository.list()`.
   - Commands are enqueued only on manual activation requests (`/activate/{signal_id}`) or when `enqueue_all_ready()` processes approved execution plans.
   - Without qualified, risk-approved assessments, no commands are enqueued, resulting in an empty list.
3. **Startup Recovery Barrier**:
   - When `execution_enabled` is `True`, startup recovery is required. If `automation_ready` is `False`, the guarded facade `LeaderValidatedExecutionService` overrides the status state to `EXECUTION_LOCKED` and blocks all plans by marking them as `BLOCKED` with a reason `RECOVERY_NOT_COMPLETE`.

### 2.2 Truthful Status Fields Solution (Option A)
To allow the frontend to display a single, coherent, and truthful message instead of two contradictory ones, we added:
- `execution_integration_ready: bool`: Indicates whether all backend configurations, API connectivity, Take Profit policies, and startup recovery steps are fully prepared to receive and execute signals.
- `execution_unavailable_reason: str | None`: A clear, user-friendly message explaining why execution is not yet integrated or ready (e.g. startup recovery in progress, credentials missing, or execution disabled).

### 2.3 Status Evaluation Logic
1. **Base Service (`DemoExecutionService.status()`)**:
   - Checks `execution_enabled` (if `False`, reason is `"Demo execution is disabled in settings."`).
   - Checks `demo_credentials_configured` (if `False`, reason is `"Binance Demo credentials are not configured."`).
   - Checks `private_api_available` (if `False`, reason is `"Binance demo private API client is not configured."`).
   - Checks `execution_take_profit_r_multiple` (if `<= 0`, reason is `"Take Profit policy is not configured."`).
   - Checks risk/state ready state (if not `READY`, reason details the current state and risk engine state).
2. **Guarded Service (`RecoveryGuardedExecutionService.status()`)**:
   - Overrides integration status if startup recovery is required but not yet completed:
     - Sets `execution_integration_ready = False`.
     - If `recovery_error` is present, sets reason to `"Startup recovery failed: <error>"`.
     - Otherwise, sets reason to `"Startup recovery is in progress or required (state: <recovery_state>)."`.

## 3. Verification Evidence

### 3.1 Unit Tests Executed
We added comprehensive unit testing to `tests/unit/test_execution.py` covering all scenario paths:
- `test_execution_integration_status_fields`:
  1. **Scenario 1: Execution Disabled**: Verifies `execution_integration_ready: False` with `"Demo execution is disabled in settings."`
  2. **Scenario 2: Missing Credentials**: Verifies `execution_integration_ready: False` with `"Binance Demo credentials are not configured."`
  3. **Scenario 3: Private Client None**: Verifies `execution_integration_ready: False` with `"Binance demo private API client is not configured."`
  4. **Scenario 4: Missing Take Profit Policy**: Verifies `execution_integration_ready: False` with `"Take Profit policy is not configured."`
  5. **Scenario 5: Fully Ready Base Service**: Verifies `execution_integration_ready: True` with `None` reason.
- `test_recovery_guarded_integration_status_fields`:
  6. **Scenario 6: Recovery Required but Not Complete**: Verifies `execution_integration_ready: False` with `"Startup recovery is in progress or required (state: RECOVERY_REQUIRED)."`
  7. **Scenario 7: Recovery Failed**: Verifies `execution_integration_ready: False` with `"Startup recovery failed: EXCHANGE_RECONCILIATION_FAILED"`
  8. **Scenario 8: Recovery Completed**: Verifies `execution_integration_ready: True` with `None` reason.

All unit tests passed perfectly.

### 3.2 Linter and Type-Checking
- **Ruff**: Passed with zero errors. All code conforms to strict 100-character line limit.
- **Mypy**: Passed on `app` with zero typing errors.

## 4. Conclusion
This audit confirms that the backend's empty plans/commands behavior is correct and fail-closed. With the addition of the explicit integration status contracts in `DemoExecutionStatusResponse`, the frontend can now truthfully and consistently convey the system state to the user without contradiction.
