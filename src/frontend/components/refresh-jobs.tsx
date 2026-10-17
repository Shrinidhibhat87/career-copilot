"use client";

import { useEffect, useRef, useState } from "react";

import {
  BILLED_ROLES,
  dismissCloudNotice,
  getCloudNotice,
  getRuns,
  previewRun,
  refreshRun,
  startRun,
  type CloudNotice,
  type RunPreview,
} from "@/lib/api";
import { euros } from "@/lib/money";
import { isActive, watchRun } from "@/lib/run-watch";

const message = (error: unknown) =>
  error instanceof Error ? error.message : "The request failed.";

const PROVIDER_NAMES: Record<string, string> = { openrouter: "OpenRouter" };
const providerName = (kind: string) => PROVIDER_NAMES[kind] ?? kind;

type Approval = { preview: RunPreview; notice: CloudNotice };

/** "Look for new jobs": starts a refresh, waits for it, then asks the list to reload and
 * says how many ids it had not shown before. With a paid model it first asks for approval,
 * and the first time says that the text is redacted and then sent to the provider. */
export function RefreshJobs({
  knownIds,
  onRefetch,
  pollMs = 3000,
}: {
  knownIds: number[];
  /** Reloads the list and resolves with the ids it now holds. */
  onRefetch: () => Promise<number[]>;
  pollMs?: number;
}) {
  const [searching, setSearching] = useState(false);
  const [result, setResult] = useState("");
  const [approval, setApproval] = useState<Approval | null>(null);
  const [remind, setRemind] = useState(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // A reload keeps the run going on the server, so pick it back up. The list was not
  // loaded when it started, so there is no "before" to count new jobs against.
  useEffect(() => {
    void getRuns()
      .then(async (rows) => {
        const active = rows.find(isActive);
        if (!active || !alive.current) return;
        setSearching(true);
        try {
          await follow(active.runId, null);
        } finally {
          if (alive.current) setSearching(false);
        }
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, on mount
  }, []);

  async function follow(runId: number, before: Set<number> | null) {
    const run = await watchRun(runId, { pollMs, isAlive: () => alive.current });
    if (run === null) return;
    const ids = await onRefetch();
    if (!alive.current) return;
    if (before === null) {
      setResult("Search finished");
      return;
    }
    const found = ids.filter((id) => !before.has(id)).length;
    setResult(found ? `Found ${found} new` : "Nothing new found");
  }

  async function look() {
    setSearching(true);
    setResult("");
    try {
      const started = await refreshRun(false);
      if (started.started && started.runId !== undefined) {
        await follow(started.runId, new Set(knownIds));
      } else if (started.code === BILLED_ROLES) {
        // A paid model is configured: no run starts without a preview and approval.
        const [preview, notice] = await Promise.all([previewRun(), getCloudNotice()]);
        if (!alive.current) return;
        setRemind(false);
        setApproval({ preview, notice });
      } else {
        setResult(started.reason ?? "No search was started.");
      }
    } catch (failure) {
      if (alive.current) setResult(message(failure));
    } finally {
      if (alive.current) setSearching(false);
    }
  }

  async function approve() {
    if (!approval) return;
    const { preview, notice } = approval;
    const before = new Set(knownIds);
    setApproval(null);
    setSearching(true);
    setResult("");
    try {
      if (notice.show) await dismissCloudNotice(remind);
      const started = await startRun(preview);
      await follow(started.runId, before);
    } catch (failure) {
      if (alive.current) setResult(message(failure));
    } finally {
      if (alive.current) setSearching(false);
    }
  }

  return (
    <span className="refresh-jobs">
      <button
        aria-busy={searching}
        className="refresh-button"
        disabled={searching}
        type="button"
        onClick={() => void look()}
      >
        <svg
          aria-hidden="true"
          className={searching ? "refresh-icon spinning" : "refresh-icon"}
          fill="none"
          height="15"
          stroke="currentColor"
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth="2"
          viewBox="0 0 24 24"
          width="15"
        >
          <path d="M21 12a9 9 0 1 1-2.64-6.36" />
          <path d="M21 3v6h-6" />
        </svg>
        Look for new jobs
      </button>
      {searching ? <span className="meta">Searching…</span> : null}
      {result ? (
        <span aria-live="polite" className="meta">
          {result}
        </span>
      ) : null}
      {approval ? (
        <div aria-label="Run on a cloud model" className="refresh-confirm" role="dialog">
          {approval.notice.show ? (
            <>
              <p>
                {approval.notice.redactionEnabled
                  ? "Your name, email, phone and address are redacted before anything is sent. "
                  : "Redaction is off, so your resume goes out as it is. "}
                The redacted text is then sent to{" "}
                {approval.notice.providers.map(providerName).join(", ") || "your model provider"}.
              </p>
              <label>
                <input
                  checked={remind}
                  type="checkbox"
                  onChange={(event) => setRemind(event.target.checked)}
                />{" "}
                Remind me next time
              </label>
            </>
          ) : null}
          <p>
            <strong>{approval.preview.candidatesNow} candidates now</strong>, about{" "}
            {approval.preview.estimatedCalls} model calls. The run stops at{" "}
            {euros(approval.preview.ceilingEur)}.
          </p>
          {!approval.preview.modelBackendReady ? (
            <p role="alert">
              {approval.preview.modelBackendKeyMissing
                ? "Add an OpenRouter key in Settings to run in the cloud."
                : "A model backend is not available. Check Models in Settings."}
            </p>
          ) : null}
          <button
            className="refresh-button"
            disabled={!approval.preview.modelBackendReady}
            type="button"
            onClick={() => void approve()}
          >
            Approve {euros(approval.preview.ceilingEur)} and run
          </button>{" "}
          <button type="button" onClick={() => setApproval(null)}>
            Cancel
          </button>
        </div>
      ) : null}
    </span>
  );
}
