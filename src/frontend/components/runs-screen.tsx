"use client";

import { useEffect, useState } from "react";
import {
  getModelHealth, getRun, getRuns, needsApproval, previewRun, startRun, type PipelineRun,
  type ModelRole, type RunPreview,
} from "@/lib/api";
import { ModelRoleFlow } from "@/components/agent-flow";
import { Disclosure } from "@/components/disclosure";
import { euros } from "@/lib/money";
import { ObservabilitySection } from "@/components/observability-section";
import { ResultsTable } from "@/components/results-table";
import { hasRunDetails, RunFunnelDetails } from "@/components/run-funnel";

// The switch is the user's: these only point at the Models section above.
const SUGGEST_COPY = {
  hybrid: "Switch to Hybrid in Models: triage stays local, the rest uses OpenRouter",
  cloud: "Switch to Cloud in Models",
  add_key: "Add an OpenRouter key in Models to run in the cloud instead",
} as const;
// Migration 4 backfilled every pre-existing row with kind 'discover', which is a default,
// not a record of what those runs did. Shown as "—" rather than as a claim.
const LEGACY_KIND = "discover";
const kindOf = (run: PipelineRun) => (run.kind === LEGACY_KIND ? "—" : run.kind);
const message = (error: unknown) => error instanceof Error ? error.message : "The request failed.";

export function RunsScreen() {
  const [preview, setPreview] = useState<RunPreview | null>(null);
  const [run, setRun] = useState<PipelineRun | null>(null);
  const [history, setHistory] = useState<PipelineRun[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [paid, setPaid] = useState(false);
  const [roles, setRoles] = useState<ModelRole[]>([]);

  // The approval card only means something when a run can cost money. A failed health read
  // hides it rather than blocking the page: the server still refuses a start without a token.
  useEffect(() => {
    let disposed = false;
    void getModelHealth().then((found) => { if (!disposed) { setPaid(needsApproval(found)); setRoles(found); } })
      .catch(() => {});
    return () => { disposed = true; };
  }, []);

  useEffect(() => {
    let disposed = false;
    void getRuns().then((rows) => {
      if (disposed) return;
      setHistory(rows);
      // Only a running row is adopted as the live run. Falling back to rows[0] is what
      // rendered a finished, migration-backfilled row as "Run 5 · succeeded · Starting ·
      // 0 / pending · €0.0000": a progress panel describing a run that ended long ago.
      setRun(rows.find((item) => item.state === "running") ?? null);
    }).catch((failure) => { if (!disposed) setError(message(failure)); })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, []);

  const activeId = run?.state === "running" ? run.runId : null;
  // Only a run the user opened (or just started): a fresh page, or a user with no runs,
  // shows no details until they click one in Run history.
  const detailRun = run && hasRunDetails(run) ? run : null;
  useEffect(() => {
    if (activeId === null) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await getRun(activeId);
        if (disposed) return;
        setRun(next);
        setError("");
        setHistory((rows) => [next, ...rows.filter((item) => item.runId !== next.runId)]);
        if (next.state !== "running") {
          setRefresh((value) => value + 1);
          return;
        }
      } catch (failure) {
        if (!disposed) setError(`${message(failure)} Progress will reconnect automatically.`);
      }
      if (!disposed) timer = setTimeout(() => void poll(), 1000);
    };
    timer = setTimeout(() => void poll(), 1000);
    return () => { disposed = true; clearTimeout(timer); };
  }, [activeId]);

  async function prepare() {
    setBusy(true); setError(""); setPreview(null);
    try { setPreview(await previewRun()); }
    catch (failure) { setError(message(failure)); }
    finally { setBusy(false); }
  }

  async function confirm() {
    if (!preview) return;
    setBusy(true); setError("");
    try {
      const started = await startRun(preview);
      // Keep polling even if the immediate status read is unavailable.
      setRun({ runId: started.runId, state: "running", kind: "full", stage: "discover",
        completed: 0, total: 0, costEur: 0, budgetEur: preview.ceilingEur, stalled: false,
        failureCategory: null, failureMessage: null, sourceFailures: [],
        startedAt: new Date().toISOString(), endedAt: null });
      setPreview(null);
    } catch (failure) {
      setError(`${message(failure)} Refresh the preview before starting again.`);
      setPreview(null);
      // Recover an active run started in another tab or after a lost POST response.
      const rows = await getRuns().catch(() => null);
      if (rows) {
        setHistory(rows);
        setRun(rows.find((item) => item.state === "running") ?? null);
      }
    } finally { setBusy(false); }
  }

  const errorLine = error ? <p className="form-error" role="alert">{error}</p> : null;

  return <>
    {paid ? <section className="run-card" aria-labelledby="run-heading">
      <h2 id="run-heading">Your next run</h2>
      <p>Uses your saved preferences. Discovery may find more jobs before scoring begins.</p>
      <button className="primary-action" type="button" disabled={busy || loading || activeId !== null} onClick={() => void prepare()}>
        {busy ? "Working…" : "Preview run"}
      </button>
      {preview && !preview.modelBackendReady ? <div className="run-confirmation" role="alert" aria-label="Models not ready">
        <h3>Can’t run yet</h3>
        <p>{preview.modelBackendKeyMissing
          ? "Add an OpenRouter key to run in the cloud, or switch to local models."
          : "A model backend isn’t available."}</p>
        <p><a className="suggest-link" href="#model-setup">{
          preview.modelBackendSuggest ? SUGGEST_COPY[preview.modelBackendSuggest] : "Open Models settings"
        }</a></p>
        {roles.length > 0 ? <Disclosure level="group" title="Model calls">
          <ModelRoleFlow roles={roles} />
        </Disclosure> : null}
        <div className="run-actions">
          <button type="button" onClick={() => setPreview(null)}>Cancel</button>
        </div>
      </div> : null}
      {preview && preview.modelBackendReady ? <div className="run-confirmation" aria-label="Spending confirmation">
        <h3>Approve up to {euros(preview.ceilingEur)}</h3>
        <p>{preview.candidatesNow} candidates, about {preview.estimatedCalls} model calls. The run stops at {euros(preview.ceilingEur)} and keeps partial results.</p>
        <div className="run-actions">
          <button className="primary-action" type="button" disabled={busy} onClick={() => void confirm()}>
            Approve {euros(preview.ceilingEur)} and run
          </button>
          <button type="button" disabled={busy} onClick={() => setPreview(null)}>Cancel</button>
        </div>
      </div> : null}
      {errorLine}
    </section> : errorLine}

    {run ? <section className="run-card" aria-labelledby="progress-heading" aria-live="polite">
      <h2 id="progress-heading">Run {run.runId} · {run.state}</h2>
      <p>{run.stage || "Starting"} · {run.completed} / {run.total || "pending"}</p>
      {run.total > 0 ? <progress aria-label="Stage progress" max={run.total} value={run.completed} /> : null}
      <p><strong>{euros(run.costEur)}</strong> spent of {euros(run.budgetEur)} approved.</p>
      {run.stalled ? <p className="form-error">Progress is delayed. The heartbeat is stale; the run may still be working.</p> : null}
      {run.state === "aborted" ? <p>Partial results have been kept. {run.failureCategory === "shutdown" ? "The server is shutting down." : "The spending ceiling was reached."}</p> : null}
      {run.failureMessage ? <p>{run.failureCategory}: {run.failureMessage}</p> : null}
      {run.state === "succeeded" ? <p>Your results are ready below.</p> : null}
      {run.sourceFailures.length ? <div><h3>Some sources could not be fetched</h3><p>Jobs from other sources were kept.</p>
        <ul>{run.sourceFailures.map((failure) => <li key={`${failure.source}:${failure.identifier}`}>{failure.source} / {failure.identifier}: {failure.message}</li>)}</ul>
      </div> : null}
    </section> : null}

    {/* Keyed by run, so opening another run shows its details expanded again. */}
    {detailRun ? <section className="run-card" aria-label="Run details" key={detailRun.runId}>
      <Disclosure defaultOpen headingId="details-heading" title="How long it took">
        <RunFunnelDetails run={detailRun} />
      </Disclosure>
    </section> : null}

    <ObservabilitySection />
    <section className="run-card" aria-label="Results">
      <Disclosure headingId="results-heading" title="Results">
        <ResultsTable refresh={refresh} />
      </Disclosure>
    </section>
    <section className="run-card" aria-label="Run history">
      <Disclosure headingId="history-heading" title="Run history">
        {loading ? <p role="status">Loading run history…</p>
          : !history.length ? <p className="settings-note">No runs yet.</p> : <ul className="run-history">
            {history.map((item) => <li key={item.runId} data-state={item.state}>
              <button type="button" disabled={activeId !== null && activeId !== item.runId} onClick={() => setRun(item)}>
                Run {item.runId} · {kindOf(item)} · {item.state}
              </button><time dateTime={item.startedAt}>{new Date(item.startedAt).toLocaleString()}</time>
            </li>)}
          </ul>}
      </Disclosure>
    </section>
  </>;
}
