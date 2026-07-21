# AstraForge Production Workflow

This document is the single source of truth for AstraForge production changes.

## Ownership

- Frontend source: `zahirulca24-bit/AstraForge-Crypto-Frontend`
- Frontend production host: Vercel
- Backend source: `zahirulca24-bit/AstraForge-Crypto`
- Backend production host: Render
- Database: Supabase/PostgreSQL
- Market and demo trading integration: Binance
- GitHub is the source of truth for code.

## Required change order

When a change affects the frontend/backend API contract:

1. Define and implement the backend contract first.
2. Run backend CI: Ruff, Mypy, Pytest/coverage, smoke test, container build.
3. Merge/deploy backend to Render.
4. Verify the live backend endpoint and response contract.
5. Update frontend types/mapping against the exact deployed contract.
6. Run frontend CI: TypeScript check and production build.
7. Merge/deploy frontend to Vercel.
8. Verify live routes, API connectivity, CORS, and the affected user workflow.
9. Mark the task DONE only after runtime proof.

## Changes that do not affect the API contract

Frontend-only changes may proceed through frontend CI and Vercel without a backend release.
Backend-only internal changes may proceed through backend CI and Render without a frontend release when the public contract is unchanged.

## Safety gates

- Do not enable real trading.
- Do not change execution/risk/strategy rules without explicit owner approval.
- Do not invent frontend fields that the backend does not return.
- Do not remove or rename backend fields until dependent frontend code is migrated.
- A successful commit, CI run, or deployment is not sufficient proof by itself; verify live runtime behavior.

## Production completion rule

A task is 100% complete only when all applicable stages are proven:

`Code -> CI -> Merge -> Deploy -> Live API/UI -> Runtime workflow proof`
