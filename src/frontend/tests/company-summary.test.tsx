import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CompanySummary } from "@/components/company-summary";

function mockFetch(...responses: Partial<Response>[]) {
  const fetchMock = vi.fn();
  for (const response of responses) fetchMock.mockResolvedValueOnce(response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const summary = (overrides: object = {}) => ({
  ok: true,
  status: 200,
  json: async () => ({
    listSource: "notion",
    counts: { resolved: 21, unresolved: 63, blocked: 2, inactive: 0 },
    openRoles: 989,
    foundWithoutRoles: 2,
    companies: [
      { name: "Esri", status: "resolved", boardType: "greenhouse", openRoles: 447, lastError: null },
      { name: "Ghost Co", status: "unresolved", boardType: null, openRoles: null, lastError: "no public board found" },
    ],
    failures: [],
    ...overrides,
  }),
});

const run = (state: string, extra: object = {}) => ({
  ok: true,
  status: 200,
  json: async () => ({
    runId: 7,
    state,
    kind: "company_sync",
    stage: "resolving",
    completed: 0,
    total: 0,
    costEur: 0,
    budgetEur: 0,
    stalled: false,
    failureCategory: null,
    failureMessage: null,
    sourceFailures: [],
    startedAt: "2026-10-06T10:00:00+00:00",
    endedAt: null,
    ...extra,
  }),
});

describe("company summary", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
  });

  it("renders nothing when no list is attached", async () => {
    const fetchMock = mockFetch(summary({ listSource: null, companies: [] }));
    const { container } = render(<CompanySummary />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(container).toBeEmptyDOMElement();
  });

  it("still shows a board added by name when no list is attached", async () => {
    mockFetch(
      summary({
        listSource: null,
        counts: { resolved: 1, unresolved: 0, blocked: 0, inactive: 0 },
        openRoles: 4,
        foundWithoutRoles: 0,
        companies: [
          { id: 0, name: "Helsing", manual: true, status: "resolved", boardType: "ashby", openRoles: 4, lastError: null },
        ],
      }),
    );
    render(<CompanySummary />);
    expect(await screen.findByText(/1 job boards found/)).toBeInTheDocument();
  });

  it("reloads the counts when a board is added", async () => {
    const fetchMock = mockFetch(summary(), summary({ openRoles: 1000 }));
    const { rerender } = render(<CompanySummary refreshKey={0} />);
    await screen.findByText(/989 open roles/);

    rerender(<CompanySummary refreshKey={1} />);

    expect(await screen.findByText(/1000 open roles/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("says the counts are being worked out when the first read is slow", async () => {
    // Never resolves: the skeleton is what the space shows in the meantime.
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
    render(<CompanySummary />);
    expect(
      await screen.findByText(/Working out how your company list resolved/, undefined, {
        timeout: 3000,
      }),
    ).toBeInTheDocument();
  });

  it("states what the list turned into without a remembered sync", async () => {
    mockFetch(summary());
    render(<CompanySummary />);
    expect(
      await screen.findByText(/Included in your search: 21 job boards found · 989 open roles · 65 companies not found/),
    ).toBeInTheDocument();
    expect(screen.getByText(/2 found boards have no open roles/)).toBeInTheDocument();
  });

  it("shows the donut once the list is in, and hides it while syncing", async () => {
    window.sessionStorage.setItem("companySyncRunId", "7");
    mockFetch(run("syncing", { completed: 1, total: 2 }), run("succeeded"), summary());
    render(<CompanySummary />);
    await screen.findByText(/Syncing your company list/);
    expect(screen.queryByLabelText(/companies by job-board status/)).not.toBeInTheDocument();
    expect(screen.getByText(/Working out how your company list resolved/)).toBeInTheDocument();
    expect(
      await screen.findByLabelText("2 companies by job-board status", undefined, { timeout: 3000 }),
    ).toBeInTheDocument();
  });

  it("follows the sync the CV page started, then shows the result", async () => {
    window.sessionStorage.setItem("companySyncRunId", "7");
    const fetchMock = mockFetch(run("syncing", { completed: 4, total: 86 }), run("succeeded"), summary());
    render(<CompanySummary />);

    expect(await screen.findByText("Syncing your company list… 4 of 86 companies")).toBeInTheDocument();
    expect(fetchMock.mock.calls[0][0]).toContain("/api/runs/7");
    expect(
      await screen.findByText(/21 job boards found · 989 open roles/, undefined, { timeout: 3000 }),
    ).toBeInTheDocument();
  });

  it("shows the sync as under way immediately, before the first poll answers", async () => {
    window.sessionStorage.setItem("companySyncRunId", "7");
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {}))); // never answers
    render(<CompanySummary />);
    expect(await screen.findByText("Syncing your company list…")).toBeInTheDocument();
  });

  it("says so when the sync failed", async () => {
    window.sessionStorage.setItem("companySyncRunId", "7");
    mockFetch(run("failed", { failureMessage: "Notion unreachable" }), summary());
    render(<CompanySummary />);
    expect(await screen.findByRole("alert")).toHaveTextContent("The company sync failed: Notion unreachable");
  });
});
