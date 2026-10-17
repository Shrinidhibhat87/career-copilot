import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { formatSeconds, hasRunDetails, RunFunnelDetails } from "@/components/run-funnel";
import type { PipelineRun } from "@/lib/api";

const BASE: PipelineRun = {
  runId: 36,
  state: "succeeded",
  kind: "full",
  stage: "digest",
  completed: 1,
  total: 1,
  costEur: 0,
  budgetEur: 1,
  stalled: false,
  failureCategory: null,
  failureMessage: null,
  sourceFailures: [],
  startedAt: "2026-10-09T20:00:00Z",
  endedAt: "2026-10-09T20:07:00Z",
};

const DETAILED: PipelineRun = {
  ...BASE,
  stages: [
    { stage: "discover", startedAt: "", endedAt: "", seconds: 12.3 },
    { stage: "score", startedAt: "", endedAt: null, seconds: null },
  ],
  funnel: {
    discover: {
      targets: 8,
      searched: 6,
      fetched: 345,
      inserted: 40,
      rejected: 290,
      review: 15,
      qualified: 28,
      rejectReasons: [{ reason: "location outside Germany", count: 250 }],
      boards: [{ target: "greenhouse:helsing", fetched: 100 }],
      skipped: [{ target: "greenhouse:n26", reason: "cooldown" }],
      failed: [],
    },
    score: {
      candidates: 39,
      capped: 19,
      matched: 19,
      above: 18,
      target: 50,
      minScore: 30,
      failed: 1,
      stoppedBy: "out of candidates",
    },
  },
};

describe("RunFunnelDetails", () => {
  it("draws stage times as a bar with a legend", () => {
    render(<RunFunnelDetails run={DETAILED} />);
    expect(screen.getByRole("img", { name: "Time spent in each stage" })).toBeInTheDocument();
    // Once in the legend and once as the headline total.
    expect(screen.getAllByText("12.3 s")).toHaveLength(2);
    expect(screen.getByText(/in total/)).toBeInTheDocument();
    expect(screen.getByText(/^Run 36 · started/)).toBeInTheDocument();
    expect(screen.getByText("did not finish")).toBeInTheDocument();
  });

  it("shows three donuts and a result card with the count, bar and stop reason", () => {
    render(<RunFunnelDetails run={DETAILED} />);
    expect(screen.getByRole("img", { name: "2 sources, split by outcome" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "345 jobs, split by outcome" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "39 to score, split by outcome" })).toBeInTheDocument();
    const bar = screen.getByRole("progressbar", { name: "18 of 50 recommendations" });
    expect(bar).toHaveAttribute("aria-valuenow", "18");
    expect(screen.getByText("Ran out of jobs to score")).toBeInTheDocument();
  });

  it("explains a high rejection share with the reasons behind it", () => {
    render(<RunFunnelDetails run={DETAILED} />);
    const jobs = screen.getByRole("region", { name: "Jobs fetched" });
    fireEvent.click(within(jobs).getByRole("button", { name: /Rejected/ }));
    expect(within(jobs).getByText(/A high share here is normal/)).toBeInTheDocument();
    expect(within(jobs).getByText("location outside Germany — 250 jobs")).toBeInTheDocument();
  });

  it("opens a slice's names when its legend row is clicked, and closes it again", () => {
    render(<RunFunnelDetails run={DETAILED} />);
    const sources = screen.getByRole("region", { name: "Sources" });
    fireEvent.click(within(sources).getByRole("button", { name: /Skipped/ }));
    expect(within(sources).getByText("greenhouse:n26 — cooldown")).toBeInTheDocument();
    fireEvent.click(within(sources).getByRole("button", { name: "Close details" }));
    expect(within(sources).queryByText("greenhouse:n26 — cooldown")).not.toBeInTheDocument();
  });

  it("shows nothing for a run recorded before stages and funnels existed", () => {
    const { container } = render(<RunFunnelDetails run={BASE} />);
    expect(container).toBeEmptyDOMElement();
    expect(hasRunDetails(BASE)).toBe(false);
    expect(hasRunDetails(DETAILED)).toBe(true);
  });

  it("writes long stages as minutes", () => {
    expect(formatSeconds(17.1)).toBe("17.1 s");
    expect(formatSeconds(401)).toBe("6 min 41 s");
  });
});
