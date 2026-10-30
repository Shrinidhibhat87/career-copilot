"use client";

import { useEffect, useState } from "react";

import { Disclosure } from "@/components/disclosure";
import {
  getSourceHealth,
  resumeSource,
  testSource,
  type SourceHealthRow,
  type SourceStatus,
} from "@/lib/api";

const STATUS_LABEL: Record<SourceStatus, string> = {
  ok: "Working",
  degraded: "Having trouble",
  paused: "Paused",
  unknown: "Not tried yet",
};

function when(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

const message = (failure: unknown) =>
  failure instanceof Error ? failure.message : "Something went wrong.";

/** Which job sources the crawler reads, whether each one is answering, and why not when it
 * is not. A source that keeps failing is paused so it stops being asked; Test sends one small
 * request and changes nothing, and Retry now makes the next run try a paused source again.
 *
 * Opens itself when a source needs attention, and stays shut when everything works. */
export function SourceHealth() {
  const [rows, setRows] = useState<SourceHealthRow[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  // What the last Test or Retry on each source said, shown beside its buttons.
  const [notes, setNotes] = useState<Record<string, { text: string; ok: boolean }>>({});

  useEffect(() => {
    let current = true;
    getSourceHealth()
      .then((loaded) => current && setRows(loaded))
      .catch((reason: unknown) => current && setError(message(reason)));
    return () => {
      current = false;
    };
  }, []);

  async function reload() {
    try {
      setRows(await getSourceHealth());
      setError("");
    } catch (failure) {
      setError(message(failure));
    }
  }

  const note = (source: string, text: string, ok: boolean) =>
    setNotes((current) => ({ ...current, [source]: { text, ok } }));

  async function test(source: string) {
    setBusy((current) => ({ ...current, [source]: true }));
    try {
      const result = await testSource(source);
      note(
        source,
        result.ok
          ? `Answered: ${result.fetched} found in ${result.durationMs} ms.`
          : `Did not work: ${result.message ?? "no reason given"}.`,
        result.ok,
      );
    } catch (failure) {
      note(source, message(failure), false);
    } finally {
      setBusy((current) => ({ ...current, [source]: false }));
    }
  }

  async function retry(source: string) {
    setBusy((current) => ({ ...current, [source]: true }));
    try {
      await resumeSource(source);
      note(source, "The next run will try it again.", true);
      await reload();
    } catch (failure) {
      note(source, message(failure), false);
    } finally {
      setBusy((current) => ({ ...current, [source]: false }));
    }
  }

  const needsAttention = rows?.some((row) => row.status === "paused" || row.status === "degraded");

  return (
    <section className="run-card source-health" aria-label="Job sources">
      <Disclosure
        forceOpen={Boolean(needsAttention) || error !== ""}
        info="Each job source is a service the crawler reads jobs from. After repeated failures a source is paused so it is not asked again for a while, then tried once more."
        title="Job sources"
      >
        {error ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}
        {rows && rows.length === 0 ? (
          <p>No job sources yet. Add a company board, or save a city in your preferences.</p>
        ) : null}
        {rows && rows.length > 0 ? (
          <table className="obs-table source-table">
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col">Status</th>
                <th scope="col">Boards</th>
                <th scope="col">Last success</th>
                <th scope="col">Jobs, 7 days</th>
                <th scope="col">Check</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.source}>
                  <th scope="row">{row.source}</th>
                  <td>
                    <span className="source-status" data-status={row.status}>
                      {STATUS_LABEL[row.status]}
                    </span>
                    {row.status === "paused" ? (
                      <small>Until {when(row.pausedUntil)}</small>
                    ) : null}
                    {row.lastMessage && row.status !== "ok" ? (
                      <small>Last problem: {row.lastMessage}</small>
                    ) : null}
                  </td>
                  <td>{row.targets}</td>
                  <td>{when(row.lastOkAt)}</td>
                  <td>{row.fetchedLast7Days}</td>
                  <td>
                    <div className="source-actions">
                      <button
                        aria-label={`Test ${row.source}`}
                        disabled={busy[row.source]}
                        onClick={() => void test(row.source)}
                        type="button"
                      >
                        {busy[row.source] ? "Working…" : "Test"}
                      </button>
                      {row.status === "paused" ? (
                        <button
                          aria-label={`Retry ${row.source} now`}
                          disabled={busy[row.source]}
                          onClick={() => void retry(row.source)}
                          type="button"
                        >
                          Retry now
                        </button>
                      ) : null}
                    </div>
                    {notes[row.source] ? (
                      <small
                        className={notes[row.source].ok ? "source-note" : "source-note source-bad"}
                        role="status"
                      >
                        {notes[row.source].text}
                      </small>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </Disclosure>
    </section>
  );
}
