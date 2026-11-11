import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ApplicationsPage from "@/app/applications/page";
import type { Pipeline, PipelineCard } from "@/lib/api";

function respond(status: number, body?: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function card(overrides: Partial<PipelineCard> = {}): PipelineCard {
  return {
    jobId: 1,
    title: "Data Engineer",
    company: "Acme GmbH",
    location: "Berlin",
    url: "https://x.test/1",
    stage: "applied",
    appliedOn: "2026-10-01T09:00:00+00:00",
    lastChange: "2026-10-01T09:00:00+00:00",
    daysQuiet: 3,
    stale: false,
    note: "",
    hidden: false,
    notionError: null,
    inNotion: false,
    ...overrides,
  };
}

function board(overrides: Partial<Pipeline> = {}): Pipeline {
  return {
    applied: [],
    interviewing: [],
    closed: { offers: [], folded: [] },
    hidden: [],
    staleDays: 14,
    ...overrides,
  };
}

/** Serves `current` for the board and records every other request. */
function serve(current: Pipeline) {
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const path = new URL(input, "http://local").pathname;
    if (path === "/api/pipeline" && !init?.method) return respond(200, current);
    if (path.endsWith("/timeline"))
      return respond(200, [{ stage: "applied", note: null, at: "2026-10-01T09:00:00+00:00" }]);
    return respond(200, card());
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const sent = (fetchMock: ReturnType<typeof serve>, method: string, suffix: string) =>
  fetchMock.mock.calls.filter(
    ([input, init]) => init?.method === method && String(input).endsWith(suffix),
  );

describe("applications board", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("says so when nothing was applied to", async () => {
    serve(board());
    render(<ApplicationsPage />);
    expect(await screen.findByText(/Nothing applied yet/)).toBeInTheDocument();
  });

  it("puts each card in its column with a count", async () => {
    serve(
      board({
        applied: [card()],
        interviewing: [card({ jobId: 2, title: "ML Engineer", stage: "interviewing" })],
      }),
    );
    render(<ApplicationsPage />);
    const applied = await screen.findByRole("region", { name: "Applied" });
    expect(within(applied).getByText("Data Engineer")).toBeInTheDocument();
    const interviewing = screen.getByRole("region", { name: "Interviewing" });
    expect(within(interviewing).getByText("ML Engineer")).toBeInTheDocument();
  });

  it("shows offers in Closed and folds rejected and withdrawn until asked", async () => {
    serve(
      board({
        closed: {
          offers: [card({ jobId: 3, title: "Offer Co role", stage: "offer" })],
          folded: [card({ jobId: 4, title: "Rejected role", stage: "rejected" })],
        },
      }),
    );
    render(<ApplicationsPage />);
    const closed = await screen.findByRole("region", { name: "Closed" });
    expect(within(closed).getByText("Offer Co role")).toBeInTheDocument();
    expect(within(closed).queryByText("Rejected role")).not.toBeInTheDocument();
    fireEvent.click(within(closed).getByRole("button", { name: "Show 1 rejected or withdrawn" }));
    expect(within(closed).getByText("Rejected role")).toBeInTheDocument();
    expect(within(closed).getAllByText("Rejected").length).toBeGreaterThan(0);
  });

  it("moves a card with its menu", async () => {
    const fetchMock = serve(board({ applied: [card()] }));
    render(<ApplicationsPage />);
    fireEvent.change(await screen.findByLabelText("Move Data Engineer"), {
      target: { value: "interviewing" },
    });
    await waitFor(() => expect(sent(fetchMock, "POST", "/pipeline/1/stage")).toHaveLength(1));
    expect(JSON.parse(String(sent(fetchMock, "POST", "/pipeline/1/stage")[0][1]?.body))).toEqual({
      stage: "interviewing",
    });
  });

  it("asks for the outcome when a card is dropped on Closed", async () => {
    const fetchMock = serve(board({ applied: [card()] }));
    render(<ApplicationsPage />);
    await screen.findByText("Data Engineer");
    const dataTransfer = { getData: () => "1", setData: () => undefined };
    fireEvent.drop(screen.getByRole("region", { name: "Closed" }), { dataTransfer });
    const group = await screen.findByRole("group", { name: "Close Data Engineer as" });
    expect(sent(fetchMock, "POST", "/pipeline/1/stage")).toHaveLength(0);
    fireEvent.click(within(group).getByRole("button", { name: "Offer" }));
    await waitFor(() => expect(sent(fetchMock, "POST", "/pipeline/1/stage")).toHaveLength(1));
  });

  it("moves a card dropped on another open column straight away", async () => {
    const fetchMock = serve(board({ applied: [card()] }));
    render(<ApplicationsPage />);
    await screen.findByText("Data Engineer");
    const dataTransfer = { getData: () => "1", setData: () => undefined };
    fireEvent.drop(screen.getByRole("region", { name: "Interviewing" }), { dataTransfer });
    await waitFor(() => expect(sent(fetchMock, "POST", "/pipeline/1/stage")).toHaveLength(1));
  });

  it("hides a card and restores it from the Hidden list", async () => {
    const fetchMock = serve(board({ hidden: [card({ jobId: 5, title: "Parked role", hidden: true })] }));
    render(<ApplicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Hidden (1)" }));
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(sent(fetchMock, "POST", "/pipeline/5/unhide")).toHaveLength(1));
  });

  it("hides from the card", async () => {
    const fetchMock = serve(board({ applied: [card()] }));
    render(<ApplicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Hide" }));
    await waitFor(() => expect(sent(fetchMock, "POST", "/pipeline/1/hide")).toHaveLength(1));
  });

  it("flags a quiet application and a failed Notion update", async () => {
    serve(
      board({
        applied: [card({ stale: true, daysQuiet: 16, notionError: "Notion unreachable (ConnectError)" })],
      }),
    );
    render(<ApplicationsPage />);
    expect(await screen.findByText("Quiet for 16 days")).toBeInTheDocument();
    expect(screen.getByText(/Notion was not updated: Notion unreachable/)).toBeInTheDocument();
  });

  it("shows the timeline and saves a note", async () => {
    const fetchMock = serve(board({ applied: [card()] }));
    render(<ApplicationsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    expect(await screen.findByRole("list", { name: "Timeline" })).toHaveTextContent("Applied");
    fireEvent.change(screen.getByLabelText("Note"), { target: { value: "Call Anna" } });
    fireEvent.click(screen.getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(sent(fetchMock, "PUT", "/pipeline/1/note")).toHaveLength(1));
  });

  it("links the Excel download and saves the quiet threshold", async () => {
    const fetchMock = serve(board({ applied: [card()] }));
    render(<ApplicationsPage />);
    expect(await screen.findByRole("link", { name: "Download Excel" })).toHaveAttribute(
      "href",
      expect.stringContaining("/api/pipeline/export.xlsx"),
    );
    fireEvent.change(screen.getByLabelText("Quiet after days"), { target: { value: "30" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(sent(fetchMock, "PUT", "/pipeline/settings")).toHaveLength(1));
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
  });
});
