# SignalLog

SignalLog is a local-only prototype for tracking check results and summarizing reliability over time. It uses SQLite and is intended for unauthenticated, localhost-only use.

## Local setup

SignalLog uses SQLite exclusively; no database server is required. The app
creates `signallog.db` in the repository root. Back up that file before
upgrading an existing database. Optional local settings can be configured by
copying `.env.example` to `.env` if one does not already exist, then editing it.

Run these commands from the repository root.

**Windows (PowerShell):**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m alembic upgrade head
```

To create the optional settings file without overwriting an existing one:

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

**Linux/macOS:**

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e ".[dev]"
./.venv/bin/python -m alembic upgrade head
```

To create the optional settings file without overwriting an existing one:

```bash
test -f .env || cp .env.example .env
```

## Known limitations

- No authentication; local use only.
- SQLite is the only supported database backend.
- No idempotency keys.
- Offset pagination can shift under concurrent writes.
- No alerting or consecutive-failure logic.
- Postgres support and a router/service-layer split are deferred.
- No retention or archival policy for results. Twenty checks at one-minute
  intervals generate about 864,000 results in 30 days; retention is the next
  data-growth concern.
- Automated checks currently support HTTP/HTTPS GET only, with public hosts on
  standard ports (80/443); no TCP, script, browser, or private-network checks.
- Results can also be submitted manually. The monitor worker must be running
  for scheduled checks.

## Definitions

- An automated result passes only when the HTTP response status is 2xx.
- Redirects are not followed. A 3xx response fails and explains that the final
  URL should be configured directly.
- 4xx, 5xx, connection errors, timeouts, and DNS errors fail. Status codes are
  recorded when an HTTP response exists; DNS and network failures have no
  status code.
- `latency_ms` measures an automated check from validation/DNS lookup through
  its HTTP response or failure. Checks stopped by the overall 10-second
  timeout record 10,000 ms. The latest-results table shows both fields;
  manual results have no HTTP status code or latency.
- The dashboard pass rate combines manual and automated results. The overview
  also returns separate manual and automated totals, passes, and failures.
  Historical rows predating these fields keep null status/latency values; they
  remain part of the existing pass-rate calculation.

## Quick start

**Windows (PowerShell):**

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

**Linux/macOS:**

```bash
./.venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

In a separate terminal at the repository root, start the single monitor
worker:

**Windows (PowerShell):**

```powershell
.\.venv\Scripts\python.exe -m app.monitor
```

**Linux/macOS:**

```bash
./.venv/bin/python -m app.monitor
```

Run the Alembic migration command in the setup section before starting the API.
The API and monitor share the same project-root SQLite database.

The worker checks configured public HTTP/HTTPS endpoints every 60 seconds by
default. Intervals can be set per check from 60 seconds to 24 hours. It does
not follow redirects, permits only ports 80 and 443, and rejects private or
non-public destination addresses to reduce SSRF risk. Keep the API and worker
on trusted local infrastructure; neither service has authentication.

## Next.js dashboard

In a second terminal:

```bash
cd web
npm ci
npm run dev
```

Open `http://localhost:3000`. Create a check with an HTTP or HTTPS target URL
to enable automatic monitoring; checks without a target remain manual-only.
The dashboard defaults to the API at `http://127.0.0.1:8000`; set
`NEXT_PUBLIC_API_BASE_URL` in `web/.env.local` only if you need to change it.
The API allows browser requests from `http://localhost:3000`.

## Tests

Run the backend test suite with:

**Windows (PowerShell):**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_signallog.py -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe app tests
```

**Linux/macOS:**

```bash
./.venv/bin/python -m pytest tests/test_signallog.py -q
./.venv/bin/ruff check .
./.venv/bin/mypy app tests
```

Tests use an isolated in-memory SQLite database and do not modify the local
`signallog.db`.

For the dashboard:

```bash
cd web
npm ci
npm run typecheck
npm run build
```

The backend and web checks run in GitHub Actions on pushes and pull requests.

## API notes

- All datetimes are UTC and returned as ISO 8601 with offset.
- Range filters use half-open intervals: `[from, to)`.
- Errors use the standard envelope:

```json
{ "error": { "code": "validation_error", "message": "...", "details": [] } }
```
