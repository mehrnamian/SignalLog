"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";

type Check = {
  id: number;
  name: string;
  description: string | null;
  target_url: string | null;
  interval_seconds: number;
  next_check_at: string | null;
  last_checked_at: string | null;
  is_disabled: boolean;
  created_at: string;
  updated_at: string;
};

type Result = {
  id: number;
  check_id: number;
  status: "passed" | "failed";
  source: "manual" | "monitor";
  message: string | null;
  status_code: number | null;
  latency_ms: number | null;
  recorded_at: string;
};

type Page<T> = {
  items: T[];
  limit: number;
  offset: number;
  total: number;
};

type Overview = {
  total: number;
  passed: number;
  failed: number;
  pass_rate: number | null;
  manual: {
    total: number;
    passed: number;
    failed: number;
  };
  monitor: {
    total: number;
    passed: number;
    failed: number;
  };
  recent_failures: {
    check_id: number;
    check_name: string;
    message: string | null;
    recorded_at: string;
  }[];
};

type ApiError = {
  error?: {
    code?: string;
    message?: string;
    details?: string[];
  };
};

const API_BASE = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const MONITOR_INTERVALS = [
  { seconds: 60, label: "1 minute" },
  { seconds: 120, label: "2 minutes" },
  { seconds: 300, label: "5 minutes" },
  { seconds: 600, label: "10 minutes" },
  { seconds: 900, label: "15 minutes" },
  { seconds: 1800, label: "30 minutes" },
  { seconds: 3600, label: "1 hour" },
  { seconds: 14400, label: "4 hours" },
  { seconds: 43200, label: "12 hours" },
  { seconds: 86400, label: "24 hours" },
];

function initialRange() {
  const to = nextMinute(new Date());
  const from = new Date(to.getTime() - 30 * 24 * 60 * 60 * 1000);
  return {
    from: localInputValue(from),
    to: localInputValue(to),
  };
}

function nextMinute(value: Date) {
  const rounded = new Date(value);
  rounded.setSeconds(0, 0);
  rounded.setMinutes(rounded.getMinutes() + 1);
  return rounded;
}

function localInputValue(value: Date) {
  const offset = value.getTimezoneOffset() * 60_000;
  return new Date(value.getTime() - offset).toISOString().slice(0, 16);
}

function toUtcIso(value: string) {
  return new Date(value).toISOString();
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
      cache: "no-store",
    });
  } catch {
    throw new Error(
      `Could not reach the API at ${API_BASE}. Make sure FastAPI is running and open the dashboard at http://localhost:3000.`,
    );
  }
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as ApiError | null;
    const message = body?.error?.message ?? `Request failed (${response.status})`;
    const details = body?.error?.details?.filter(Boolean).join(" ");
    throw new Error(details ? `${message}: ${details}` : message);
  }
  return response.json() as Promise<T>;
}

export default function Home() {
  const [range, setRange] = useState(initialRange);
  const [customRange, setCustomRange] = useState(false);
  const [checkFilter, setCheckFilter] = useState("");
  const [overview, setOverview] = useState<Overview | null>(null);
  const [checks, setChecks] = useState<Check[]>([]);
  const [results, setResults] = useState<Result[]>([]);
  const [checkTotal, setCheckTotal] = useState(0);
  const [resultTotal, setResultTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [savingCheck, setSavingCheck] = useState(false);
  const [savingResult, setSavingResult] = useState(false);

  const checkNames = useMemo(
    () => new Map(checks.map((check) => [check.id, check.name])),
    [checks],
  );

  const loadDashboard = useCallback(async (backgroundRefresh = false) => {
    if (!backgroundRefresh) setLoading(true);
    setError("");
    try {
      const selectedRange = customRange ? range : initialRange();
      if (!customRange) {
        setRange((current) =>
          current.from === selectedRange.from && current.to === selectedRange.to
            ? current
            : selectedRange,
        );
      }
      const params = new URLSearchParams({
        from: toUtcIso(selectedRange.from),
        to: toUtcIso(selectedRange.to),
      });
      if (checkFilter) params.set("check_id", checkFilter);
      const [summary, checkPage, resultPage] = await Promise.all([
        request<Overview>(`/overview?${params}`),
        request<Page<Check>>("/checks?limit=100&offset=0"),
        request<Page<Result>>("/results?limit=10&offset=0"),
      ]);
      setOverview(summary);
      setChecks(checkPage.items);
      setCheckTotal(checkPage.total);
      setResults(resultPage.items);
      setResultTotal(resultPage.total);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not load SignalLog.");
    } finally {
      if (!backgroundRefresh) setLoading(false);
    }
  }, [checkFilter, customRange, range]);

  useEffect(() => {
    void loadDashboard();
    const timer = window.setInterval(() => void loadDashboard(true), 15_000);
    return () => window.clearInterval(timer);
  }, [loadDashboard]);

  async function createCheck(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    setSavingCheck(true);
    setError("");
    setNotice("");
    const form = new FormData(formElement);
    try {
      await request<Check>("/checks", {
        method: "POST",
        body: JSON.stringify({
          name: String(form.get("name") ?? ""),
          description: String(form.get("description") ?? ""),
          target_url: String(form.get("target_url") ?? ""),
          interval_seconds: Number(form.get("interval_seconds") ?? 60),
        }),
      });
      formElement.reset();
      setNotice("Check created.");
      await loadDashboard();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not create check.");
    } finally {
      setSavingCheck(false);
    }
  }

  async function submitResult(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    setSavingResult(true);
    setError("");
    setNotice("");
    const form = new FormData(formElement);
    try {
      await request<Result>("/results", {
        method: "POST",
        body: JSON.stringify({
          check_id: Number(form.get("check_id")),
          status: String(form.get("status")),
          message: String(form.get("message") ?? ""),
        }),
      });
      formElement.reset();
      setNotice("Result recorded.");
      if (!customRange) {
        setRange((current) => ({
          ...current,
          to: localInputValue(nextMinute(new Date())),
        }));
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not record result.");
    } finally {
      setSavingResult(false);
    }
  }

  async function toggleCheck(check: Check) {
    setError("");
    setNotice("");
    try {
      await request<Check>(`/checks/${check.id}`, {
        method: "PATCH",
        body: JSON.stringify({ is_disabled: !check.is_disabled }),
      });
      setNotice(`${check.name} ${check.is_disabled ? "enabled" : "disabled"}.`);
      await loadDashboard();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not update check.");
    }
  }

  async function updateInterval(check: Check, intervalSeconds: number) {
    setError("");
    setNotice("");
    try {
      await request<Check>(`/checks/${check.id}`, {
        method: "PATCH",
        body: JSON.stringify({ interval_seconds: intervalSeconds }),
      });
      setNotice(`Monitoring interval updated for ${check.name}.`);
      await loadDashboard();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not update monitoring interval.");
    }
  }

  const activeChecks = checks.filter((check) => !check.is_disabled);

  return (
    <main className="shell">
      <header className="topbar">
        <Link className="brand" href="/" aria-label="SignalLog home">
          <span className="brand-mark">S</span>
          <span>Signal<span className="brand-light">Log</span></span>
        </Link>
        <div className="topbar-right">
          <span className="local-pill"><span className="live-dot" /> Local workspace</span>
          <button className="icon-button" type="button" onClick={() => void loadDashboard()} aria-label="Refresh dashboard">
            ↻
          </button>
        </div>
      </header>

      <section className="intro">
        <div>
          <p className="eyebrow">RELIABILITY OVERVIEW</p>
          <h1>Signals at a glance.</h1>
          <p className="subheading">A clear view of your checks, their outcomes, and the latest failures.</p>
        </div>
        <div className="range-control">
          <label>
            <span>From</span>
            <input type="datetime-local" value={range.from} onChange={(event) => {
              setCustomRange(true);
              setRange((old) => ({ ...old, from: event.target.value }));
            }} />
          </label>
          <span className="range-arrow">→</span>
          <label>
            <span>To</span>
            <input type="datetime-local" value={range.to} onChange={(event) => {
              setCustomRange(true);
              setRange((old) => ({ ...old, to: event.target.value }));
            }} />
          </label>
          <label className="custom-range-toggle">
            <input
              type="checkbox"
              checked={customRange}
              onChange={(event) => {
                setCustomRange(event.target.checked);
                if (!event.target.checked) setRange(initialRange());
              }}
            />
            <span>Custom range</span>
          </label>
          <label className="filter-select">
            <span>Check</span>
            <select value={checkFilter} onChange={(event) => setCheckFilter(event.target.value)}>
              <option value="">All checks</option>
              {checks.map((check) => <option key={check.id} value={check.id}>{check.name}</option>)}
            </select>
          </label>
        </div>
      </section>

      {error && <div className="alert alert-error" role="alert">{error}</div>}
      {notice && <div className="alert alert-success" role="status">{notice}</div>}

      <section className="metrics-grid" aria-label="Reliability metrics">
        <article className="metric-card">
          <div className="metric-label">TOTAL RESULTS <span className="metric-icon">↗</span></div>
          <strong>{loading ? "—" : overview?.total ?? 0}</strong>
          <span className="metric-foot">
            {loading ? "In the selected period" : `Manual ${overview?.manual.total ?? 0} · Automatic ${overview?.monitor.total ?? 0}`}
          </span>
        </article>
        <article className="metric-card">
          <div className="metric-label">PASS RATE <span className="metric-icon green">✓</span></div>
          <strong>{loading ? "—" : overview?.pass_rate == null ? "—" : `${overview.pass_rate.toFixed(2)}%`}</strong>
          <span className="metric-foot">{overview?.pass_rate == null && !loading ? "No data for this period" : "Passed results / total results"}</span>
        </article>
        <article className="metric-card">
          <div className="metric-label">PASSED <span className="metric-icon green">✓</span></div>
          <strong>{loading ? "—" : overview?.passed ?? 0}</strong>
          <span className="metric-foot">Successful results</span>
        </article>
        <article className="metric-card">
          <div className="metric-label">FAILED <span className="metric-icon red">!</span></div>
          <strong>{loading ? "—" : overview?.failed ?? 0}</strong>
          <span className="metric-foot">Needs your attention</span>
        </article>
      </section>

      <section className="content-grid">
        <article className="panel checks-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">MONITORING</p>
              <h2>Your checks <span className="count-badge">{checkTotal}</span></h2>
            </div>
          </div>
          {checks.length === 0 ? (
            <div className="empty-state">
              <span className="empty-icon">◉</span>
              <strong>No checks yet</strong>
              <span>Create a check below to start recording results.</span>
            </div>
          ) : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>CHECK</th><th>STATUS</th><th>TARGET</th><th>INTERVAL</th><th /></tr></thead>
                <tbody>
                  {checks.map((check) => (
                    <tr key={check.id}>
                      <td><span className="check-avatar">{check.name.slice(0, 1).toUpperCase()}</span><span className="check-name">{check.name}</span></td>
                      <td><span className={`status ${check.is_disabled ? "status-disabled" : "status-active"}`}><span />{check.is_disabled ? "Disabled" : "Active"}</span></td>
                      <td className="description-cell target-cell" title={check.target_url ?? undefined}>{check.target_url || "Manual only"}</td>
                      <td className="interval-cell">
                        {check.target_url ? (
                          <select
                            aria-label={`Monitoring interval for ${check.name}`}
                            value={check.interval_seconds}
                            onChange={(event) => void updateInterval(check, Number(event.target.value))}
                          >
                            {MONITOR_INTERVALS.map((option) => (
                              <option value={option.seconds} key={option.seconds}>{option.label}</option>
                            ))}
                          </select>
                        ) : "—"}
                      </td>
                      <td className="action-cell">
                        <button className="text-button" type="button" onClick={() => void toggleCheck(check)}>{check.is_disabled ? "Enable" : "Disable"}</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </article>

        <article className="panel failures-panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">ATTENTION</p>
              <h2>Recent failures <span className="count-badge">{overview?.recent_failures.length ?? 0}</span></h2>
            </div>
            <span className="failure-mark">!</span>
          </div>
          {overview?.total === 0 ? (
            <div className="empty-state compact"><strong>No data for this period</strong><span>Try a wider date range.</span></div>
          ) : !overview?.recent_failures.length ? (
            <div className="empty-state compact"><strong>All clear</strong><span>No failures in this period.</span></div>
          ) : (
            <ul className="failure-list">
              {overview.recent_failures.map((failure, index) => (
                <li key={`${failure.check_id}-${failure.recorded_at}-${index}`}>
                  <span className="failure-line" />
                  <div className="failure-copy">
                    <strong>{failure.check_name}</strong>
                    <span>{failure.message || "No additional details"}</span>
                    <time dateTime={failure.recorded_at}>{formatDate(failure.recorded_at)}</time>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </article>
      </section>

      <section className="bottom-grid">
        <article className="panel form-panel">
          <div className="panel-heading">
            <div><p className="eyebrow">GET STARTED</p><h2>Create a check</h2></div>
            <span className="step-number">01</span>
          </div>
          <form onSubmit={createCheck} className="form-stack">
            <label>Check name<input name="name" required minLength={1} maxLength={120} placeholder="e.g. API availability" /></label>
            <label>HTTP or HTTPS URL<input name="target_url" type="url" required maxLength={2048} placeholder="https://example.com/health" /></label>
            <div className="form-row">
              <label>Interval
                <select name="interval_seconds" defaultValue={60}>
                  {MONITOR_INTERVALS.map((option) => (
                    <option value={option.seconds} key={option.seconds}>{option.label}</option>
                  ))}
                </select>
              </label>
              <label>Description <span className="optional">OPTIONAL</span><input name="description" maxLength={500} placeholder="What does this check monitor?" /></label>
            </div>
            <button className="primary-button" disabled={savingCheck}>{savingCheck ? "Creating…" : "Create check"} <span>→</span></button>
          </form>
        </article>

        <article className="panel form-panel">
          <div className="panel-heading">
            <div><p className="eyebrow">MANUAL ENTRY</p><h2>Record a result</h2></div>
            <span className="step-number">02</span>
          </div>
          {activeChecks.length === 0 ? (
            <div className="form-hint">Create or enable a check before recording a result.</div>
          ) : (
            <form onSubmit={submitResult} className="form-stack">
              <label>Check<select name="check_id" required>{activeChecks.map((check) => <option value={check.id} key={check.id}>{check.name}</option>)}</select></label>
              <div className="form-row">
                <label>Outcome<select name="status"><option value="passed">Passed</option><option value="failed">Failed</option></select></label>
                <label>Message <span className="optional">OPTIONAL</span><input name="message" maxLength={1000} placeholder="Short context" /></label>
              </div>
              <button className="primary-button" disabled={savingResult}>{savingResult ? "Recording…" : "Record result"} <span>→</span></button>
            </form>
          )}
        </article>
      </section>

      <section className="panel results-panel">
        <div className="panel-heading">
          <div><p className="eyebrow">ACTIVITY</p><h2>Latest results <span className="count-badge">{resultTotal}</span></h2></div>
          <span className="results-caption">Showing latest 10</span>
        </div>
        {results.length === 0 ? (
          <div className="empty-state compact"><strong>No results recorded</strong><span>Submit a manual result to see activity here.</span></div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>CHECK</th><th>OUTCOME</th><th>SOURCE</th><th>HTTP</th><th>LATENCY</th><th>MESSAGE</th><th>RECORDED</th></tr></thead>
              <tbody>
                {results.map((result) => (
                  <tr key={result.id}>
                    <td><span className="check-name">{checkNames.get(result.check_id) ?? `Check #${result.check_id}`}</span></td>
                    <td><span className={`result-badge ${result.status}`}>{result.status}</span></td>
                    <td><span className="source-label">{result.source === "monitor" ? "Automatic" : "Manual"}</span></td>
                    <td>{result.status_code ?? "—"}</td>
                    <td>{result.latency_ms == null ? "—" : `${result.latency_ms} ms`}</td>
                    <td className="description-cell">{result.message || "—"}</td>
                    <td className="time-cell">{formatDate(result.recorded_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <footer><span>SignalLog <span className="footer-dot">·</span> Local reliability signals</span><span>Built for clarity, not noise.</span></footer>
    </main>
  );
}
