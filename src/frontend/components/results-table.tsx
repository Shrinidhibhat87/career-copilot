"use client";

import { useEffect, useState } from "react";

import { getJobs, type JobsPage } from "@/lib/api";

type Sort = "score" | "triage" | "title" | "company" | "age" | "cost";

function score(value: number | null) {
  return value === null ? "—" : String(value);
}

export function ResultsTable({ refresh }: { refresh: number }) {
  const [data, setData] = useState<JobsPage>({ items: [], total: 0, page: 1, limit: 20 });
  const [outcome, setOutcome] = useState("");
  const [sort, setSort] = useState<Sort>("score");
  const [direction, setDirection] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(1);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let disposed = false;
    void getJobs(outcome, sort, direction, page)
      .then((result) => {
        if (!disposed) {
          setData(result);
          setError("");
        }
      })
      .catch((failure: unknown) => {
        if (!disposed) {
          setError(failure instanceof Error ? failure.message : "Results could not be loaded.");
        }
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [direction, outcome, page, refresh, sort]);

  function changeSort(next: Sort) {
    setLoading(true);
    setPage(1);
    if (sort === next) setDirection((value) => (value === "desc" ? "asc" : "desc"));
    else {
      setSort(next);
      setDirection(next === "title" || next === "company" ? "asc" : "desc");
    }
  }

  const pages = Math.max(1, Math.ceil(data.total / data.limit));
  const heading = (label: string, value: Sort) => (
    <button className="table-sort" type="button" onClick={() => changeSort(value)}>
      {label}{sort === value ? (direction === "desc" ? " ↓" : " ↑") : ""}
    </button>
  );

  return (
    <section className="results-card" aria-labelledby="results-heading">
      <div className="results-heading">
        <div>
          {/* The heading is the Settings page's "Results" section toggle (`results-heading`). */}
          <p>Filter outcomes are deterministic. Triage and match scores come from models.</p>
        </div>
        <label>
          Outcome
          <select
            value={outcome}
            onChange={(event) => {
              setLoading(true);
              setOutcome(event.target.value);
              setPage(1);
            }}
          >
            <option value="">All</option>
            <option value="pass">Pass</option>
            <option value="manual_review">Manual review</option>
            <option value="reject">Reject</option>
          </select>
        </label>
      </div>
      {error ? <p className="form-error" role="alert">{error}</p> : null}
      <div className="results-scroll">
        <table>
          <thead><tr>
            <th>{heading("Job", "title")}</th>
            <th>{heading("Company", "company")}</th>
            <th>Location</th><th>Source</th><th>{heading("Seen", "age")}</th>
            <th className="deterministic-column">Outcome</th>
            <th className="model-column">{heading("Triage", "triage")}</th>
            <th className="model-column">{heading("Match", "score")}</th>
            <th className="model-column">{heading("Cost", "cost")}</th>
          </tr></thead>
          <tbody>
            {data.items.map((job) => <tr key={job.id}>
              <td><a href={job.url} rel="noreferrer" target="_blank">{job.title}</a></td>
              <td>{job.company}</td><td>{job.location || "—"}</td><td>{job.source}</td>
              <td><time dateTime={job.firstSeenAt}>{new Date(job.firstSeenAt).toLocaleDateString()}</time></td>
              <td className="deterministic-column"><span className={`outcome outcome-${job.outcome}`}>{job.outcome.replace("_", " ")}</span></td>
              <td className="model-column">{score(job.triageScore)}</td>
              <td className="model-column">{score(job.matchScore)}</td>
              <td className="model-column">{job.costEur === null ? "—" : `€${job.costEur.toFixed(4)}`}</td>
            </tr>)}
          </tbody>
        </table>
      </div>
      {!loading && data.items.length === 0 ? <p>No jobs match this filter yet.</p> : null}
      {loading ? <p role="status">Loading results…</p> : null}
      <nav className="pagination" aria-label="Results pages">
        <button type="button" disabled={page <= 1 || loading} onClick={() => { setLoading(true); setPage(page - 1); }}>Previous</button>
        <span>Page {page} of {pages} · {data.total} jobs</span>
        <button type="button" disabled={page >= pages || loading} onClick={() => { setLoading(true); setPage(page + 1); }}>Next</button>
      </nav>
    </section>
  );
}
