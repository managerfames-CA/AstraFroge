## AstraForge Backend PR Checklist

- [ ] API contract changes are explicit: endpoint, method, request fields, response fields, and status codes.
- [ ] Backward compatibility or required frontend follow-up is documented.
- [ ] Ruff passes.
- [ ] Mypy passes.
- [ ] Pytest passes with coverage gate.
- [ ] FastAPI import smoke test passes.
- [ ] Container build passes.
- [ ] Render backend deployment is successful before any dependent frontend release.
- [ ] Live API/runtime behavior is verified after deploy.
- [ ] Real trading/execution safety settings remain unchanged unless explicitly approved by the owner.

## Frontend contract impact

State whether the frontend must change. If yes, list the exact API contract changes the frontend must consume.

## Runtime proof

Describe the live verification performed after deployment. A code push or green CI result alone is not considered DONE.
