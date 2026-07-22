# BE-21: Strict Mypy Verification Evidence

## Before Mypy Output

Initially, running strict Mypy across `app/` and `tests/` resulted in **159 type errors** across **27 files**.

```text
Found 159 errors in 27 files (checked 87 source files)
```

Sample of type errors from tests:
```text
tests/unit/test_scanner_runtime.py:5: error: Module "tests.unit.scanner_test_support" does not explicitly export attribute "ScannerDirection"  [attr-defined]
tests/unit/test_scanner_runtime.py:5: error: Module "tests.unit.scanner_test_support" does not explicitly export attribute "ScannerEngine"  [attr-defined]
tests/unit/test_scanner_runtime.py:392: error: Incompatible types in assignment (expression has type "def evaluate_setups(self, loaded: EvaluationContext) -> tuple[list[SetupMatch], list[ScannerEvaluationError]]", variable has type "def evaluate_setups(self, ctx: EvaluationContext) -> tuple[list[SetupMatch], list[ScannerEvaluationError]]")  [assignment]
tests/unit/test_persistence_adapters.py:81: error: Unsupported operand types for <= ("int" and "None")  [operator]
tests/unit/test_trade_management.py:290: error: Item "None" of "DemoTradeRecord | None" has no attribute "lifecycle"  [union-attr]
tests/unit/test_order_reconciliation.py:97: error: Incompatible return value type (got "list[dict[str, str]]", expected "list[dict[str, object]]")  [return-value]
```

---

## After Mypy Output

After resolving all type annotation mismatches, adding explicit re-exports (via `__all__`), properly narrowing optional/union types (via `is not None` assertions), resolving unreachable statement assertions, and cleaning up unused `type: ignore` comments, running Mypy results in **0 errors / 0 warnings**:

```text
$ poetry run mypy app/ tests/
Success: no issues found in 206 source files
```

---

## Changes and Verifications

1. **Explicit Exports**: Defined `__all__` in `tests/unit/scanner_test_support.py` and `app/api/v1/dependencies.py` to prevent implicit re-export failures under strict mode.
2. **Proper Type Narrowing**: Added explicit `is not None` checks and assertions to safely narrow Union types (e.g., `DemoTradeRecord | None` and `str | None` status codes) without resorting to mass `# type: ignore` silencers.
3. **No-Untyped-Defs**: Resolved all missing type annotations for functions, nested classes, and test fixtures (e.g., `tmp_path`, `settings`, lambda structures).
4. **Resolved Assignment/Method-Assign Mismatches**: Addressed strict SQLAlchemy/Pydantic/Enum method assignments by casting mock fields or calling through `cast(Any, ...)` wrappers.
5. **No Unreachable Blocks**: Fixed mock lease assertions so they don't trick Mypy's static analyzer into marking code as unreachable.
6. **Cleaned Redundant Ignores**: Cleared out redundant `# type: ignore` comments that became unused after these improvements.
7. **CI Protection**: Updated `.github/workflows/ci.yml` so that every future PR is fully checked and enforced under `mypy app tests`.
