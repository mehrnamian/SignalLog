# SignalLog

SignalLog is a local-only prototype for tracking check results and summarizing reliability over time. It uses SQLite and is intended for unauthenticated, localhost-only use.

## Local setup

SignalLog uses SQLite exclusively. No database server or separate database
configuration is needed: the app creates and uses `signallog.db` in the project
root. That local database file is ignored by Git and should not be committed.

```bash
cd D:\Py\SignalLog
python -m venv .venv
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\python -m pip install -e .
.venv\Scripts\alembic upgrade head
```

## Known limitations

- No authentication; local use only.
- SQLite is the only supported database backend.
- No idempotency keys.
- Offset pagination can shift under concurrent writes.
- No retention or archival policy for results.
- Automated checks currently support HTTP/HTTPS GET only, with public hosts on
  standard ports (80/443); no TCP, script, browser, or private-network checks.
- Results can also be submitted manually. The monitor worker must be running
  for scheduled checks.

## Quick start

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

In a separate terminal, start the single monitor worker:

```bash
cd D:\Py\SignalLog
.venv\Scripts\python.exe -m app.monitor
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
cd D:\Py\SignalLog\web
npm install
npm run dev
```

Open `http://localhost:3000`. Create a check with an HTTP or HTTPS target URL
to enable automatic monitoring; checks without a target remain manual-only.
The dashboard defaults to the API at `http://127.0.0.1:8000`; set
`NEXT_PUBLIC_API_BASE_URL` in `web/.env.local` only if you need to change it.
The API allows browser requests from `http://localhost:3000`.

## Tests

Run the backend test suite with:

```bash
.venv\Scripts\python.exe -m pytest tests\test_signallog.py -q
```

Tests use an isolated in-memory SQLite database and do not modify the local
`signallog.db`.

## API notes

- All datetimes are UTC and returned as ISO 8601 with offset.
- Range filters use half-open intervals: `[from, to)`.
- Errors use the standard envelope:

```json
{ "error": { "code": "validation_error", "message": "...", "details": [] } }
```
