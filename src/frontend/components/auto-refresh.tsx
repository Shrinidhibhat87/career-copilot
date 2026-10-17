"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { getRuns, refreshRun } from "@/lib/api";
import { isActive } from "@/lib/run-watch";

// Once per browser session, not once per page: every screen renders the header.
const SESSION_KEY = "career-copilot:refresh-checked";

function alreadyChecked() {
  try {
    return window.sessionStorage.getItem(SESSION_KEY) === "1";
  } catch {
    return false;
  }
}

function markChecked() {
  try {
    window.sessionStorage.setItem(SESSION_KEY, "1");
  } catch {
    // Storage blocked: the server's own staleness check still prevents repeat runs.
  }
}

/** On app open, ask the server to refresh recommendations if they are stale. Silent
 * unless a run actually started, and never an error the user has to deal with. */
export function AutoRefresh() {
  const [runId, setRunId] = useState<number | null>(null);

  useEffect(() => {
    let live = true;
    if (alreadyChecked()) {
      // A reload mid-run: the run is still going, so bring the banner back.
      getRuns()
        .then((rows) => {
          const active = rows.find(isActive);
          if (live && active) setRunId(active.runId);
        })
        .catch(() => undefined);
      return () => {
        live = false;
      };
    }
    markChecked();
    refreshRun(true)
      .then((result) => {
        if (live && result.started && result.runId !== undefined)
          setRunId(result.runId);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  if (runId === null) return null;
  return (
    <p className="auto-refresh" role="status">
      Finding today&apos;s new roles… <Link href="/settings">see progress</Link>
    </p>
  );
}
