# Project Status

Updated: 2026-10-25

## Assessment and environment

- Assessment brief: `IncuBrix_Assessment_4_Distribution_Long_Tail_Destination_Network_Brief.pdf`
- Runtime: Python 3.13.14 (meets the Python 3.11+ requirement).
- Docker: not installed or available in this environment.
- Project virtual environment: not present; required Python packages are available in the active environment.
- Starter baseline: `python -m pytest -q` — 9 passed. Latest complete suite: `python -m pytest -q` — 66 passed. Tests use local API behavior, simulations, and mocks; no external platform was contacted.

## Requirement status after audit

| Requirement | Status | Existing evidence / gap |
|---|---|---|
| Starter API, health, readiness, interactive docs | Implemented | `/`, `/health`, `/ready`, `/docs`; live endpoint check passed. |
| Versioned destination and job API | Partial | Destination create/list, certification, and job submit/status/results/evidence exist. Destination update and full configuration management are missing. |
| Exact Appendix A connector contract | Partial | Required methods and optional method behavior are contract-shape tested for all six keys. Manifests now carry documented/configurable rate policies and all shared error codes; no live platform compatibility run exists, and provider-specific request behavior remains unverified. |
| Six Core connector types | Partial | Six connector implementations/manifests are present and all required methods pass simulated tests. No real platform/API integration or certification has been performed. |
| Per-destination posting rules | Partial | Configurable frequency window/cap, self-promotion, link policy, canonical-link checks, and AI disclosure are enforced at submission and immediately before connector calls. Human-confirmed rule capture is missing. |
| Job persistence and idempotency | Partial | SQLite, queued/running/succeeded/failed states, progress, result/evidence links, idempotent requests, simulation, bounded retry, and rejection of ambiguous retries exist. There is no durable external queue; interrupted uncertain jobs are not retried. |
| Destination catalogue and CSV tooling | Partial | Exact Appendix A destination fields, CSV import, row-level validation, URL normalization, platform+URL deduplication, import reports, listing, and CSV export are implemented. Reachability, audience enrichment, human-confirmed rules capture, and the 150 researched entries are not implemented. |
| Certification and cleanup evidence | Partial | Async per-destination harness stores validate/publish/status/delete evidence and cleans up simulated posts immediately. Real runs are blocked unless an owned local target or explicitly approved sandbox is configured; no real certification has been run. Stale real runs are reconciled at startup when the target remains approved. |
| Tailoring, recommendations, metrics, recertification | Partial | Deterministic tailoring, rule-aware topic ranking, three metric adapters, and an opt-in 29-day recertification scheduler exist. No labelled precision set or real metrics/recertification run has been verified. |
| React frontend | Implemented | Responsive React/Vite dashboard covers destinations, job submission/status, catalogue import/export, tailoring/recommendations, certification initiation/status, and alerts. TypeScript/Vite production build and browser smoke flow passed. |
| Docker Compose and local test instances | Partial | API/web Dockerfiles and Compose services exist, including optional Ghost and Lemmy development targets. Docker is unavailable here, so container builds, Compose configuration, service startup, and connector connectivity remain unverified. |
| Logs and health | Implemented locally | Health/readiness exist; logs are JSON and carry correlation IDs and job IDs. |
| OpenAPI 3.1 artifact, batch runner, submission documents | Implemented | Generated `openapi.yaml`, JSON batch runner, test pack with actual local simulation output/log/index, README, REPORT, SOURCES, and AI_USE are checked in. |

## Implementation phases

1. Audit requirements and establish a passing test baseline — complete.
2. Implement connector manifests, API contract, shared error codes, and six connector types — simulated contract tests pass; real integrations remain unverified.
3. Complete rule-aware job processing, safe retry behavior, simulation, and evidence — local SQLite workflow implemented; a durable queue and external object storage remain.
4. Implement the destination catalogue and CSV workflows.
5. Implement certification and test-post cleanup evidence — simulated end-to-end harness and approval gates implemented; no real/local service certification has been performed.
6. Implement tailoring, recommendations, supported metrics, and recertification — deterministic/mock implementations exist; real metrics and scheduler outcomes remain unverified.
7. Complete the React UI, Docker/local dependency definitions, OpenAPI artifact, batch runner, tests, and submission documentation — implemented; Docker validation remains blocked by tool availability.

No real-platform publication, certification, external integration, or catalogue research has been claimed.

## Full-stack and delivery phase evidence

- Backend and batch-runner tests after the connector error/contract phase: `python -m pytest -q` — 66 passed.
- Frontend: `npm run build` — TypeScript checks and Vite production build passed.
- Browser smoke flow against an isolated local API created destinations, submitted a publication, and displayed the simulated job as succeeded at 100%. A separate certification run completed simulated validate/publish/status/delete steps, and the UI displayed its cleanup evidence.
- Batch runner was exercised against an isolated local API database. It returned success and wrote a job result/evidence JSON plus a log. The captured evidence states `simulation_mode: true` and `is_real_publication: false`; the artifacts are indexed in `test_pack/index.json`.
- `openapi.yaml` was generated from the current FastAPI application.
- API and web Dockerfiles, default Compose API/web services, and optional local Ghost/Lemmy service definitions were added. Docker is not installed in this environment; no image build, Compose startup, or local-platform integration is claimed. Compose YAML syntax and service names were parsed separately.
- The development UI proxy supports `API_PROXY_TARGET`; default remains `http://127.0.0.1:8000`.

## Certification and connector audit

- Recovered run `f63ae44c-0cd6-4aca-b9db-0feabe80032c` for `certification-smoke` from its isolated SQLite database and retrieved the same record through `GET /api/v1/certifications/{run_id}`.
- The stored run is `simulation_only`; its post ID is `sim-9f3d6e4fa230eae11a14` and URL is `mock://discourse/...`. The recorded sequence is validate, publish, get_status, delete. The simulator recorded `deleted: true`; deletion was observed approximately 0.109 seconds after run creation, before the recorded 10-minute deadline. This is simulated cleanup, not a real Discourse certification.
- Tightened cleanup request timeouts to remaining deadline, record late deletes as deadline failures, and persist unexpected cleanup exceptions as failures. Added regression coverage for ambiguous publish timeouts, cleanup failures, deadline overruns, timeout bounds, and API retrieval.
- Fixed bearer Authorization headers for Lemmy and webhook status/delete requests; regression tests verify the configured token is used.
- Focused tests after the certification safety audit: `python -m pytest tests/test_certification.py tests/test_connectors.py -q` — 31 passed before the subsequent error/contract phase.
- See the Connector error and contract phase below for updated status, including quota mapping and manifest policies.

## Connector error and contract phase

- Each connector manifest defines the Appendix A `per_connection` shape (`requests`, `per_seconds`). Discourse uses the 60/60 assessment example; the same values on the other connector manifests are configurable application defaults, not verified platform limits. The in-process HTTP request limiter applies only to real outbound HTTP connector calls; it is not distributed across workers. Assisted publication makes no outbound platform request, so its manifest entry is not a provider quota or a limiter on manual posting.
- `PLATFORM_QUOTA_EXHAUSTED` is recognized from explicit upstream error codes and mapped to the shared retryable envelope with `retry_after: next_window` and any available reset details. Job retry blocks until a reset duration has elapsed; unknown reset time requires waiting for the next quota window.
- Error mapping tests cover authentication rejection, standard 429 reset, quota exhaustion, rejected content, transport and HTTP 408 timeouts (including ambiguous POST outcomes), unavailable platform, and consistent error envelopes.
- Contract tests cover required methods for all six connectors, supported/unsupported optional methods, result keys, simulated deletion state, and manual-only assisted output.
- Live Discourse, Lemmy, Dev.to, Ghost, and partner webhook compatibility remains unverified; no external platform was contacted.
- Manifest validation confirms all six keys have non-empty per-connection policies and the shared error mappings; the entries contain only `requests` and `per_seconds`.
- `openapi.yaml` matches the current FastAPI schema. Compose YAML syntax and expected service names parse successfully, but this is not a Compose startup test.
- Docker CLI is unavailable (`Get-Command docker` returned no executable), so Compose startup and local Ghost/Lemmy integration tests could not run. Safe simulated and mocked contract tests were run instead.

## Latest phase evidence

- `python -m pytest -q` — 23 passed.
- Local tests exercise required connector method result shapes in simulation, replay behavior, assisted-pack output, manifest loading, and HMAC signing over the exact request bytes.
- The connector API defaults to simulation (`SIMULATION_MODE=true`). No external destination was contacted and no test post was published.
- Known gap: platform-specific request/response behavior, credentials, deletion support, rate-limit semantics, and metrics still require validation against owned local instances or suitable mocks. Do not call these connectors certified.

## Job-processing phase evidence

- Jobs run after HTTP acceptance in a background task and persist status/progress, result manifest, timestamps, and evidence in SQLite.
- Frequency, promotion, link/canonical-link, and disclosure checks run both before accepting a job and again before connector publication.
- Retry is permitted only for a recorded retryable failure with a known-safe outcome; rate-limit retries wait for the recorded reset duration, while ambiguous POST timeouts are explicitly blocked from retry.
- Test publications use the in-process simulation only. No real publication or duplicate-prevention behavior on a live remote platform has been verified.
- At this phase: `python -m pytest -q` — 23 passed.

## Catalogue phase evidence

- At this phase: `python -m pytest -q` — 28 passed.
- The 1,000-row CSV performance test passed locally in under one second (measured by the test; no reachability network calls are made).
- Bad rows are reported without aborting valid rows; repeated normalized URL plus platform key is reported as a duplicate.
- The project contains no researched destination list. The importer deliberately does not scrape or probe imported URLs.

## Certification phase evidence

- At this phase: `python -m pytest -q` — 36 passed.
- Local simulation exercises all six connector keys; for the five automatic connector types, the harness validates, publishes to the mock adapter, confirms mock status, and deletes the mock post in the same run. Assisted certification generates a ready-to-post pack and remains explicitly manual.
- Real certification is rejected by default and requires explicit test-environment/sandbox settings and an enabling environment variable. No real endpoint was contacted or certified.
- The ten-minute cleanup deadline is stored in every certification record. Cleanup failure or an interrupted real run is recorded as failed/ambiguous rather than reported as certified.

## Tailoring, recommendations, metrics, and recertification evidence

- Earlier phase results: 43 tests passed at the tailoring phase and 45 at initial delivery; these have been superseded by the 66-test full suite in Full-stack and delivery phase evidence.
- Tailoring uses manifest limits, word-boundary truncation, posting-rule checks, and human-review suggestions; it uses no external AI provider.
- Recommendations use supplied audience tags, catalogue relevance, current rule/frequency checks, registered certification state, and verified real-job history. `precision_at_10` is explicitly `null` because no labelled set was supplied.
- Discourse, Lemmy, and Dev.to return the Appendix A normalized metrics shape; unavailable individual metrics are `null`, not fabricated. These API mappings have mock tests only, and platform-specific `since` behavior is not yet verified.
- Recertification is opt-in (`RECERTIFICATION_ENABLED=true`), due before the configured 30-day threshold, and can demote/disable destinations with an alert when no approved sandbox is available. No scheduled real recertification has run.
