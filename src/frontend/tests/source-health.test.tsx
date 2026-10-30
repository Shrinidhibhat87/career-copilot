import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SourceHealth } from "@/components/source-health";
import type { SourceHealthRow } from "@/lib/api";

function response(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body } as Response;
}

const row = (over: Partial<SourceHealthRow>): SourceHealthRow => ({
  source: "greenhouse",
  status: "ok",
  targets: 8,
  lastOkAt: "2026-10-30T09:00:00+00:00",
  lastFailure: null,
  lastMessage: null,
  pausedUntil: null,
  fetchedLast7Days: 120,
  ...over,
});

const PAUSED = row({
  source: "workable",
  status: "paused",
  targets: 2,
  lastFailure: "rate_limited",
  lastMessage: "asked us to slow down and is being left alone for a while",
  pausedUntil: "2026-10-31T09:00:00+00:00",
  fetchedLast7Days: 0,
});

type Handlers = {
  health: () => Response;
  test?: (name: string) => Response;
  resume?: (name: string) => Response;
};

function stub(handlers: Handlers) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push(`${init?.method ?? "GET"} ${url}`);
    if (url.endsWith("/api/sources/health")) return handlers.health();
    const match = /\/api\/sources\/([^/]+)\/(test|resume)$/.exec(url);
    if (match && handlers[match[2] as "test" | "resume"]) {
      return handlers[match[2] as "test" | "resume"]!(match[1]);
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}

/** The section is shut while every source works, so a test about a working source opens it. */
async function open() {
  const toggle = await screen.findByRole("button", { name: "Job sources" });
  await waitFor(() => expect(screen.getByRole("table", { hidden: true })).toBeInTheDocument());
  if (toggle.getAttribute("aria-expanded") === "false") fireEvent.click(toggle);
}

beforeEach(() => vi.unstubAllGlobals());
afterEach(() => vi.unstubAllGlobals());

describe("SourceHealth", () => {
  it("stays shut while every source works, and opens by itself when one is paused", async () => {
    stub({ health: () => response([row({})]) });
    const { unmount } = render(<SourceHealth />);
    const toggle = await screen.findByRole("button", { name: "Job sources" });
    await waitFor(() => expect(toggle).toHaveAttribute("aria-expanded", "false"));
    unmount();

    stub({ health: () => response([PAUSED, row({})]) });
    render(<SourceHealth />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Job sources" })).toHaveAttribute(
        "aria-expanded",
        "true",
      ),
    );
  });

  it("shows each source's status, boards and jobs, and why a paused one is paused", async () => {
    stub({ health: () => response([PAUSED, row({})]) });
    render(<SourceHealth />);
    const workable = (await screen.findByRole("rowheader", { name: "workable" })).closest("tr")!;
    expect(within(workable).getByText("Paused")).toBeInTheDocument();
    expect(within(workable).getByText(/Until /)).toBeInTheDocument();
    expect(within(workable).getByText(/Last problem: asked us to slow down/)).toBeInTheDocument();
    expect(within(workable).getByRole("button", { name: "Retry workable now" })).toBeInTheDocument();

    const greenhouse = screen.getByRole("rowheader", { name: "greenhouse" }).closest("tr")!;
    expect(within(greenhouse).getByText("Working")).toBeInTheDocument();
    expect(within(greenhouse).getByText("120")).toBeInTheDocument();
    expect(within(greenhouse).queryByRole("button", { name: /retry/i })).toBeNull();
  });

  it("tests a source and says what it found", async () => {
    const { calls } = stub({
      health: () => response([row({})]),
      test: () =>
        response({ ok: true, fetched: 14, durationMs: 320, reason: null, message: null }),
    });
    render(<SourceHealth />);
    await open();
    fireEvent.click(await screen.findByRole("button", { name: "Test greenhouse" }));
    expect(await screen.findByText("Answered: 14 found in 320 ms.")).toBeInTheDocument();
    expect(calls).toContain("POST /api/sources/greenhouse/test");
  });

  it("says why a test failed, in words", async () => {
    stub({
      health: () => response([row({})]),
      test: () =>
        response({
          ok: false,
          fetched: 0,
          durationMs: 30000,
          reason: "timeout",
          message: "took too long to answer",
        }),
    });
    render(<SourceHealth />);
    await open();
    fireEvent.click(await screen.findByRole("button", { name: "Test greenhouse" }));
    expect(await screen.findByText("Did not work: took too long to answer.")).toBeInTheDocument();
  });

  it("shows the server's wording when a source was tested a moment ago", async () => {
    stub({
      health: () => response([row({})]),
      test: () =>
        response({ detail: { errors: { source: "tested a moment ago; try again in 42 s" } } }, false, 429),
    });
    render(<SourceHealth />);
    await open();
    fireEvent.click(await screen.findByRole("button", { name: "Test greenhouse" }));
    expect(await screen.findByText("tested a moment ago; try again in 42 s")).toBeInTheDocument();
  });

  it("disables a row's buttons while its test is running", async () => {
    let finish: (value: Response) => void = () => undefined;
    stub({
      health: () => response([row({})]),
      test: () => new Promise<Response>((resolve) => (finish = resolve)) as unknown as Response,
    });
    render(<SourceHealth />);
    await open();
    const button = await screen.findByRole("button", { name: "Test greenhouse" });
    fireEvent.click(button);
    await waitFor(() => expect(button).toBeDisabled());
    expect(button).toHaveTextContent("Working…");
    finish(response({ ok: true, fetched: 1, durationMs: 5, reason: null, message: null }));
    await waitFor(() => expect(button).not.toBeDisabled());
  });

  it("Retry now asks the server, then shows the source as no longer paused", async () => {
    let paused = true;
    const { calls } = stub({
      health: () => response([paused ? PAUSED : { ...PAUSED, status: "degraded", pausedUntil: null }]),
      resume: () => {
        paused = false;
        return response({ status: "degraded" });
      },
    });
    render(<SourceHealth />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry workable now" }));
    expect(await screen.findByText("The next run will try it again.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Having trouble")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Retry workable now" })).toBeNull();
    expect(calls).toContain("POST /api/sources/workable/resume");
  });

  it("says so when there are no sources yet", async () => {
    stub({ health: () => response([]) });
    render(<SourceHealth />);
    const toggle = await screen.findByRole("button", { name: "Job sources" });
    fireEvent.click(toggle);
    expect(await screen.findByText(/No job sources yet/)).toBeInTheDocument();
  });

  it("shows an error when the health list cannot be read", async () => {
    stub({ health: () => response({}, false, 500) });
    render(<SourceHealth />);
    expect(await screen.findByRole("alert")).toHaveTextContent("The request failed. Try again.");
  });
});
