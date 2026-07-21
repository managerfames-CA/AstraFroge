# AstraForge Backend Agent Control Rules

These instructions are mandatory for every coding or documentation agent working in this repository.

## Locked Project-Control Checklist

The backend master checklist is the `BE-01` through `BE-30` checklist in `README.md`.

Agents MUST NOT:

- delete the checklist;
- replace it with a phase summary, architecture narrative or a different task list;
- reorder, renumber, merge or split checklist items;
- mark an item complete from implementation claims, code presence or partial CI evidence alone;
- skip ahead and change later checklist items while an earlier item remains the active serial task, unless the repository owner explicitly authorizes an exception;
- remove or rewrite prior Completion Log evidence;
- describe the backend as production-ready while any required production-readiness item remains incomplete;
- enable live or real-money trading.

Agents MUST:

1. Work in serial order from the first unchecked `BE` item.
2. Keep all `BE-01` through `BE-30` items visible in `README.md`.
3. Change `[ ]` to `[x]` only after the item-specific code is merged into `main` and the required verification has passed against the stated commit.
4. Add or update exactly one matching Completion Log row with the item, PR, merge commit and verification evidence.
5. Keep architecture and runtime documentation, but never use it as a replacement for the locked checklist.
6. Preserve Binance USD-M Futures Demo-only boundaries and fail-closed behavior.
7. Stop and report a conflict instead of silently altering these controls.

## README Change Gate

Any PR that edits `README.md` must explicitly confirm all of the following:

- `BE-01` through `BE-30` are still present and in the original order.
- No checklist item was deleted, renumbered, merged or replaced.
- Every new `[x]` has item-specific merged-code and verification evidence.
- The Completion Log matches the checklist.
- Current Next Action points to the first unchecked item unless the owner explicitly approved another order.

A PR that fails any of these checks must not be merged.
