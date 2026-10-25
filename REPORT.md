# Assessment implementation report

## Summary

The project provides a full-stack, simulation-first application for long-tail destination publishing. It includes a FastAPI API, a React dashboard, persistent SQLite job and destination records, six connector manifests, CSV catalogue operations, rules-aware job validation, certification workflow records, content tailoring/recommendation endpoints, and a batch runner.

The system is not presented as a certified production integration. No live platform was contacted, no real post or metrics were obtained, and no researched list of 150 destinations was fabricated.

## Implemented

- FastAPI health/readiness endpoints, API documentation, correlation IDs, structured logs, and consistent error responses.
- Destination registration and rule validation, persistent job state, idempotency, retry safeguards, and result/evidence endpoints.
- Discourse, Lemmy, Dev.to, Ghost, HMAC webhook, and assisted connector manifests/adapters. Automated tests exercise simulation and mocks; live integration behavior remains unverified.
- Catalogue CSV import, row validation, normalization/deduplication, reports, listing, and export. The importer does not request external URLs.
- Certification workflow, simulated test steps, cleanup records, explicit approval gates for real certification, and an opt-in recertification scheduler.
- Deterministic tailoring and recommendations based on available catalogue data and rules. Precision-at-10 is not measurable without labelled relevance judgments.
- React dashboard for destinations, publication submission/status, catalogue import/export, tailoring, recommendations, certification initiation, and alerts.
- JSON batch runner and a local simulation fixture, Dockerfiles, Compose definitions, and environment-variable example.

## Validation evidence

- Backend and batch runner: `python -m pytest -q` — 66 passed.
- Frontend: `npm run build` — TypeScript checks and Vite production build passed.
- Browser smoke flow created destinations and submitted a job against an isolated local API; the UI displayed the simulated job as succeeded at 100%. A simulated certification run also completed validate/publish/status/delete, and its cleanup evidence was visible in the dashboard.
- Batch runner completed against an isolated local API and saved actual simulation output and logs under `test_pack/`.
- `openapi.yaml` was generated from the current FastAPI application.
- The tests exercise local database/API behavior, simulated connector contracts, error cases, and mocked metrics; they do not demonstrate live platform certification.
- Docker was unavailable in the implementation environment. Compose configuration and local Lemmy/Ghost startup therefore remain unverified.
- Static checks parsed all six manifests, confirmed `rate_limits.per_connection` contains only the Appendix A fields `requests` and `per_seconds`, verified the shared error mappings, and confirmed `openapi.yaml` matches the current FastAPI schema. `docker-compose.yml` parsed as YAML with the expected service names; Docker Compose itself was not run.
- The `certification-smoke` run was recovered from SQLite and fetched through the certification GET endpoint. It used only `mock://` Discourse simulation and recorded simulated deletion within the 10-minute deadline; it is not real certification.
- Certification audit fixes bound delete-request timeouts to the remaining deadline, fail late/unconfirmed cleanup, preserve ambiguous publish timeouts, and correct Lemmy/webhook bearer headers.
- Connector manifests now define per-connection request ceilings using the Appendix A field shape. Discourse's 60/60 is the assessment example; the other 60/60 values are configurable application defaults, not verified platform limits. The HTTP request-path limiter is process-local, not distributed. Assisted publication does not make outbound platform requests, so its entry is not a provider quota or a control on manual posting.
- `PLATFORM_QUOTA_EXHAUSTED` uses an explicit upstream code marker, returns a retryable shared error envelope, and preserves reset details when present. Retry blocks until the reported delay expires; if no reset is available, it advises waiting for the next quota window.
- Authentication rejection, 429 rate limits, quota exhaustion, rejected content, transport/HTTP 408 timeouts, and unavailable-platform errors have regression coverage, including ambiguous POST handling and unsafe retry blocks. Tests also cover optional-method support or explicit unsupported errors, six connector simulated method shapes, and assisted manual-only behavior.
- Docker executable is not installed/available on PATH, so Docker Compose and real local Ghost/Lemmy integration were not run. Live platform compatibility remains unverified.

## Remaining limitations

1. No live platform API credentials or owned targets were used. Connector request compatibility, rate limits, deletion, metrics, and delivery semantics require per-platform tests.
2. No real catalogue research or reachability/audience enrichment was performed. A catalogue import is not proof that a destination exists or accepts automated posts.
3. The local Compose platform targets are not automatically provisioned as certified destinations. Docker was unavailable; Compose startup, container health, and connectivity have not been validated.
4. Job history in the dashboard is limited to jobs known to that browser; the API currently does not expose a global job-list endpoint.
5. No labelled recommendation dataset exists, so recommendation precision cannot be claimed.
6. SQLite and FastAPI background tasks are appropriate for the assessment prototype but do not provide a durable distributed queue or production object storage.

## Safe operation

Leave `SIMULATION_MODE=true` unless a real connector target has been reviewed and explicitly approved. Real certification requires an owned instance or approved sandbox, and publication requires separate credential/configuration review. Never use development placeholder secrets outside a loopback-only local environment.

## Windows PowerShell run commands

Run from a PowerShell terminal:

```powershell
Set-Location C:\Users\keert\incubrix-distribution
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
$env:SIMULATION_MODE = "true"
python -m uvicorn app.main:app --reload
```

In a second PowerShell terminal, run the dashboard:

```powershell
Set-Location C:\Users\keert\incubrix-distribution\frontend
npm ci
npm run dev
```

Run the backend tests and production frontend build:

```powershell
Set-Location C:\Users\keert\incubrix-distribution
python -m pytest -q
Set-Location C:\Users\keert\incubrix-distribution\frontend
npm ci
npm run build
```

The intended container command is `docker compose up --build`, but it cannot run in the current environment because the Docker CLI is unavailable.
