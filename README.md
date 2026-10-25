# IncuBrix Distribution

A simulation-first distribution workflow for managing long-tail publishing destinations. The application includes a FastAPI backend, a React/Vite dashboard, SQLite persistence, connector manifests, catalogue CSV tools, and a batch job runner.

> **Important:** Connector execution defaults to simulation. The UI, test pack, and included development configuration do not publish content to external platforms. Do not enable real publication or certification without reviewing the connector implementation, destination rules, credentials handling, and target ownership.

## Requirements

- Python 3.11 or newer (verified with Python 3.13.14)
- Node.js 20 or newer and npm (verified with Node.js 22.17.0 and npm 10.9.2)
- Docker is not required for local development.

## Install dependencies on Windows

Run these once from PowerShell. The virtual environment keeps Python packages isolated from the rest of Windows:

```powershell
Set-Location C:\Users\keert\incubrix-distribution
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.txt` installs FastAPI (API framework), `uvicorn[standard]` (ASGI server), Pydantic (request/data validation), HTTPX (connector HTTP client and tests), PyYAML (connector manifests), and pytest (tests).

Install the frontend dependencies in a separate PowerShell terminal:

```powershell
Set-Location C:\Users\keert\incubrix-distribution\frontend
npm.cmd ci
```

## Run locally without Docker

Start the backend in one PowerShell terminal:

```powershell
Set-Location C:\Users\keert\incubrix-distribution
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
$env:SIMULATION_MODE = "true"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --env-file .env --host 127.0.0.1 --port 8000
```

The backend entry point is `app.main:app`. The API listens at `http://127.0.0.1:8000`; API documentation is at `http://127.0.0.1:8000/docs`, and health/readiness checks are at `/health` and `/ready`.

Start the frontend in another PowerShell terminal:

```powershell
Set-Location C:\Users\keert\incubrix-distribution\frontend
$env:API_PROXY_TARGET = "http://127.0.0.1:8000"
npm.cmd run dev -- --host 127.0.0.1 --port 5173
```

Open `http://127.0.0.1:5173`. The frontend lives in `frontend/`; its `package.json` scripts are `dev` (Vite development server), `build` (TypeScript check and production build), and `preview` (serve the built frontend). Vite proxies `/api/v1`, `/connector`, `/health`, and `/ready` to `API_PROXY_TARGET`, which defaults to `http://127.0.0.1:8000`. No frontend API base URL needs to be set for this proxied local setup.

The API reads `.env` because the command uses Uvicorn's `--env-file` option. `.env.example` documents the supported local settings; no platform credentials are needed in simulation mode. Key settings and defaults are:

- `SIMULATION_MODE=true` — keep enabled for local development; no external posts are made.
- `DATABASE_PATH=data/incubrix.db` — SQLite database path, relative to the project working directory unless an absolute path is supplied. The parent directory is created automatically.
- `LOG_LEVEL=INFO`, `MAX_CATALOGUE_IMPORT_BYTES=20000000`, `MAX_JOB_ATTEMPTS=5`, and `JOB_STALE_AFTER_SECONDS=300`.
- `RECERTIFICATION_ENABLED=false` and `RECERTIFICATION_INTERVAL_SECONDS=86400`.

If port 8000 is already occupied, use a different backend port (for example, `--port 8001`) and set `$env:API_PROXY_TARGET = "http://127.0.0.1:8001"` before starting Vite. Vite uses 5173 by default and will try a subsequent port if 5173 is occupied. Do not stop an existing process unless you know it belongs to this project.

To create a production frontend build:

```powershell
Set-Location C:\Users\keert\incubrix-distribution\frontend
npm.cmd run build
```

## Docker Compose (optional)

Docker is required only for the optional container workflow:

```powershell
Set-Location C:\Users\keert\incubrix-distribution
docker compose up --build
```

Docker and Compose startup were not verified in the available environment.

The optional `local-platforms` profile defines development Ghost and Lemmy services and their dependencies:

```powershell
Set-Location C:\Users\keert\incubrix-distribution
docker compose --profile local-platforms up --build
```

These local services are fixtures for future integration testing; they are not automatically configured as IncuBrix destinations or certified. The credentials in Compose and `docker/lemmy.hjson` are development-only placeholders. Keep the ports bound to loopback and replace all credentials before any non-local use.

## Main workflows

- **Destinations:** Register a connector key and configure a frequency cap, promotion, links, and disclosure rules.
- **Publish:** Submit content to an enabled destination. Jobs persist their state and expose result/evidence URLs. The browser tracks jobs submitted in that browser's local storage.
- **Catalogue:** Import and export the destination schema as CSV. Imports validate rows and report duplicates; imported URLs are not crawled.
- **Insights:** Request deterministic catalogue-based recommendations or tailoring suggestions. No external AI service is used.
- **Certification:** Start a connector test run. Simulated runs do not create external posts. Real test publication is gated and should only be enabled against an explicitly approved, owned target.
- **Batch runner:** Submit JSON job inputs, wait for terminal status, and save job/result/evidence JSON and a log.

## Batch runner

Create the destination referred to by the fixture using the dashboard or API, then run from the repository root:

```powershell
Set-Location C:\Users\keert\incubrix-distribution
.\.venv\Scripts\python.exe scripts\run_batch.py test_pack\inputs\publish-demo.json
```

The runner writes one JSON result per job under `test_pack/outputs/` and an execution log at `test_pack/logs/batch-runner.log`. Options include `--api-url`, `--output-dir`, `--log-file`, `--poll-seconds`, and `--timeout-seconds`. See [test_pack/README.md](test_pack/README.md) for the complete local example.

## API and configuration

- OpenAPI is generated by FastAPI at `/openapi.json`; a checked-in snapshot is [openapi.yaml](openapi.yaml).
- API routes are under `/api/v1`; connector methods are under `/connector/v1`.
- `.env.example` lists configurable environment variables. The local backend command loads the copied `.env` file with Uvicorn's `--env-file` option.
- Connector manifests live in [manifests/](manifests).
- Manifest `per_connection` limits are configurable application-side request ceilings. The 60 requests per 60 seconds value in the Discourse manifest follows Appendix A's example; values for the other connectors are configurable defaults, not verified provider quotas. The process-local limiter covers real outbound HTTP connector requests, not simulated calls or manual assisted posting. Provider quota responses are separately mapped to `PLATFORM_QUOTA_EXHAUSTED` and are not retried until their reset is known.
- API errors use a shared envelope with `code`, `message`, `retryable`, and `details`; requests include correlation IDs in response headers and structured logs.

## Validation

Run backend tests from the project root:

```powershell
Set-Location C:\Users\keert\incubrix-distribution
.\.venv\Scripts\python.exe -m pytest -q
```

Build the frontend:

```powershell
Set-Location C:\Users\keert\incubrix-distribution\frontend
npm.cmd run build
```

The automated suite uses local simulation and mocks. It does not certify real platform integrations, prove delivery behavior against a live platform, or establish recommendation precision on a labelled dataset. See [REPORT.md](REPORT.md) and [PROJECT_STATUS.md](PROJECT_STATUS.md) for the evidence and remaining limitations.

## Project documents

- [REPORT.md](REPORT.md) — implementation summary, validation evidence, and open gaps.
- [SOURCES.md](SOURCES.md) — assessment and external documentation consulted.
- [AI_USE.md](AI_USE.md) — disclosure of AI assistance and verification limits.
- [PROJECT_STATUS.md](PROJECT_STATUS.md) — requirement-by-requirement progress.
