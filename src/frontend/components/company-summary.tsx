"use client";

import { useEffect, useState, useSyncExternalStore } from "react";

import { CompanyDonut, CompanyDonutSkeleton } from "@/components/company-donut";
import { UnfoundCompanies } from "@/components/unfound-companies";
import {
  getCompanyListSummary,
  getRun,
  type CompanyListSummary,
  type PipelineRun,
} from "@/lib/api";
import { recallSyncRun } from "@/lib/sync-run";

const POLL_MS = 500;
// A local summary normally answers in a few milliseconds. Showing the skeleton only once the
// fetch is slower than this keeps a user with no list attached from seeing a donut flash and
// vanish: whether a list exists at all is not known until the response lands.
const SKELETON_AFTER_MS = 300;

// Nothing else writes the id while this page is open, so there is nothing to subscribe to.
const subscribeNothing = () => () => {};

const message = (error: unknown) => (error instanceof Error ? error.message : "Something went wrong.");

/** What the company list turned into: how many boards were found, how many open roles
 * they hold, and how many companies could not be found. Follows the sync the CV page
 * started; shows nothing when no list is attached. */
export function CompanySummary({ refreshKey = 0 }: { refreshKey?: number }) {
  const [run, setRun] = useState<PipelineRun | null>(null);
  const [summary, setSummary] = useState<CompanyListSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [slow, setSlow] = useState(false);
  // The sync the CV page started. Read through an external-store hook because session
  // storage does not exist during the static build: the server snapshot is `null`, so the
  // first client paint matches the server's and the stored id appears right after.
  const runId = useSyncExternalStore(subscribeNothing, recallSyncRun, () => null);
  // A remembered sync is in flight from the first paint here, even before the first
  // poll answers; a repeat sync can finish in well under a second.
  const following = runId !== null;

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const loadSummary = async () => {
      try {
        const loaded = await getCompanyListSummary();
        if (!disposed) {
          setSummary(loaded);
          setError(null);
        }
      } catch (failure) {
        if (!disposed) setError(message(failure));
      }
    };

    const poll = async () => {
      if (runId === null) return;
      try {
        const next = await getRun(runId);
        if (disposed) return;
        setRun(next);
        if (next.state === "syncing") {
          timer = setTimeout(() => void poll(), POLL_MS);
          return;
        }
        void loadSummary();
      } catch (failure) {
        if (disposed) return;
        setError(`${message(failure)} Progress will reconnect automatically.`);
        timer = setTimeout(() => void poll(), POLL_MS);
      }
    };

    const slowTimer = setTimeout(() => {
      if (!disposed) setSlow(true);
    }, SKELETON_AFTER_MS);

    if (runId === null) void loadSummary();
    else void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
      clearTimeout(slowTimer);
    };
  }, [runId]);

  // After a company is pointed at its board, its counts and the not-found list change.
  const reloadSummary = () =>
    void getCompanyListSummary()
      .then(setSummary)
      .catch((failure: unknown) => setError(message(failure)));

  // A board added by name changes the counts without a sync: reload when the page says so.
  useEffect(() => {
    if (refreshKey > 0) reloadSummary();
  }, [refreshKey]);

  // Boards added by name count even when no company list is attached.
  const hasBoards = summary !== null && (summary.listSource !== null || summary.companies.length > 0);
  const syncing = run?.state === "syncing" || (following && run === null && !error);
  // The first fetch has not answered, and is slow enough that the space should say so.
  const pending = summary === null && !error && !syncing && slow;
  if (!syncing && !error && !pending && !hasBoards) return null;

  const { resolved, unresolved, blocked } = summary?.counts ?? { resolved: 0, unresolved: 0, blocked: 0 };
  const notFound = unresolved + blocked;

  return (
    <section className="pref-section" aria-labelledby="company-summary-heading">
      <h2 id="company-summary-heading">Your target companies</h2>
      {syncing ? (
        <p aria-live="polite" className="live-status">
          {run && run.total > 0
            ? `Syncing your company list… ${run.completed} of ${run.total} companies`
            : "Syncing your company list…"}
        </p>
      ) : null}
      {syncing || pending ? <CompanyDonutSkeleton /> : null}
      {!syncing && run?.state === "failed" ? (
        <p className="form-error" role="alert">
          The company sync failed{run.failureMessage ? `: ${run.failureMessage}` : "."}
        </p>
      ) : null}
      {!syncing && summary && hasBoards && run?.state !== "failed" ? (
        <p aria-live="polite" className="live-status">
          Included in your search: {resolved} job boards found · {summary.openRoles} open roles ·{" "}
          {notFound} companies not found
          {summary.foundWithoutRoles > 0
            ? ` · ${summary.foundWithoutRoles} found boards have no open roles`
            : ""}
        </p>
      ) : null}
      {!syncing && summary && hasBoards && run?.state !== "failed" ? (
        <CompanyDonut companies={summary.companies} />
      ) : null}
      {!syncing && summary && hasBoards && run?.state !== "failed" ? (
        <UnfoundCompanies companies={summary.companies} onResolved={reloadSummary} />
      ) : null}
      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}
