import { getRun, type PipelineRun } from "@/lib/api";

const ACTIVE = new Set(["running", "syncing"]);

export const isActive = (run: Pick<PipelineRun, "state">) =>
  ACTIVE.has(run.state);

/** Polls a run until it is no longer running. `onProgress` sees every answer, the last one
 * included. Resolves with the finished run, or null once `isAlive` says nobody is watching. */
export async function watchRun(
  runId: number,
  {
    pollMs = 3000,
    onProgress,
    isAlive = () => true,
  }: {
    pollMs?: number;
    onProgress?: (run: PipelineRun) => void;
    isAlive?: () => boolean;
  } = {},
): Promise<PipelineRun | null> {
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, pollMs));
    if (!isAlive()) return null;
    const run = await getRun(runId);
    if (!isAlive()) return null;
    onProgress?.(run);
    if (!isActive(run)) return run;
  }
}

/** What a finished score step says about itself: the `key=value` tail of a run's summary. */
export type RunCounts = {
  candidates: number;
  matched: number;
  above: number;
  target: number;
  capped: number;
};

export function parseRunCounts(summary: string | null | undefined): RunCounts | null {
  if (!summary) return null;
  const read = (key: string) => new RegExp(`${key}=(\\d+)`).exec(summary);
  const candidates = read("candidates");
  const matched = read("matched");
  const above = /above=(\d+)\/(\d+)/.exec(summary);
  const capped = read("capped");
  if (!candidates || !matched || !above || !capped) return null;
  return {
    candidates: Number(candidates[1]),
    matched: Number(matched[1]),
    above: Number(above[1]),
    target: Number(above[2]),
    capped: Number(capped[1]),
  };
}

const plural = (count: number, word: string) =>
  `${count} ${word}${count === 1 ? "" : "s"}`;

/** The sentence the chat posts when a search it started has ended. `found` is how many
 * listed jobs the page had not shown before. */
export function describeRun(run: PipelineRun, found: number): string {
  if (run.state === "failed") {
    return `The search failed: ${run.failureMessage ?? "something went wrong"}`;
  }
  if (run.state === "aborted") return "The search was stopped before it finished.";
  const head = found
    ? `Found ${plural(found, "new role")}.`
    : "Nothing new found.";
  const counts = parseRunCounts(run.summary);
  if (!counts) return head;
  const parts = [
    `${plural(counts.matched, "role")} scored, ${counts.above} cleared the match bar.`,
  ];
  if (counts.target > 0 && counts.above >= counts.target) {
    parts.push(
      `It stopped at your limit of ${counts.target} per search (Preferences, postings scored per run).`,
    );
  }
  if (counts.capped > 0) {
    parts.push(
      `${plural(counts.capped, "role")} held back by the per-company limit (Preferences, most kept per company).`,
    );
  }
  return [head, ...parts].join(" ");
}
