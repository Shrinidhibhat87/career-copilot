"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { type BinItem, deleteForever, getBin, restoreJob } from "@/lib/api";

const DAY_MS = 86_400_000;

const days = (n: number) => `${n} ${n === 1 ? "day" : "days"}`;

function dismissedAgo(iso: string): string {
  const elapsed = Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / DAY_MS));
  return elapsed === 0 ? "Dismissed today" : `Dismissed ${days(elapsed)} ago`;
}

/** Dismissed jobs, newest first, until the bin's time runs out. Restore brings one back;
 * Delete forever cleans it now, after a confirm. */
export function RecycleBin() {
  const [items, setItems] = useState<BinItem[] | null>(null);
  const [confirming, setConfirming] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    getBin()
      .then((bin) => setItems(bin.items))
      .catch((reason: Error) => setError(reason.message));
  }, []);

  async function act(jobId: number, action: (id: number) => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await action(jobId);
      setItems((current) => current?.filter((item) => item.jobId !== jobId) ?? null);
      setConfirming(null);
    } catch (reason) {
      setError((reason as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="review-title">
        <div className="review-heading">
          <h1>Recycle bin</h1>
        </div>
        <div className="review-actions">
          <Link className="soft-link" href="/recommendations">
            Back to Recommendations
          </Link>
        </div>
      </div>
      {error ? (
        <p className="bin-error" role="alert">
          {error}
        </p>
      ) : null}
      {items === null ? null : items.length === 0 ? (
        <p className="bin-empty">Nothing here</p>
      ) : (
        <ul className="bin-list">
          {items.map((item) => (
            <li className="bin-row" key={item.jobId}>
              <div className="bin-job">
                <p className="bin-title">{item.title}</p>
                <p className="bin-company">{item.company}</p>
                <p className="bin-meta">
                  <span>{dismissedAgo(item.dismissedAt)}</span>
                  <span>{days(item.daysLeft)} left</span>
                </p>
              </div>
              {confirming === item.jobId ? (
                <div className="bin-confirm" role="group" aria-label="Confirm delete">
                  <span>Delete forever? This cannot be undone.</span>
                  <button
                    className="ghost-action bin-danger"
                    disabled={busy}
                    onClick={() => void act(item.jobId, deleteForever)}
                    type="button"
                  >
                    Yes
                  </button>
                  <button
                    className="ghost-action"
                    disabled={busy}
                    onClick={() => setConfirming(null)}
                    type="button"
                  >
                    No
                  </button>
                </div>
              ) : (
                <div className="bin-buttons">
                  <button
                    className="ghost-action"
                    disabled={busy}
                    onClick={() => void act(item.jobId, restoreJob)}
                    type="button"
                  >
                    Restore
                  </button>
                  <button
                    className="ghost-action bin-danger"
                    disabled={busy}
                    onClick={() => setConfirming(item.jobId)}
                    type="button"
                  >
                    Delete forever
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
