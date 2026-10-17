import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as Module from "@/components/auto-refresh";

// `tests/setup.ts` stubs this component for every page test; this file needs the real one.
const { AutoRefresh } = await vi.importActual<typeof Module>(
  "@/components/auto-refresh",
);

function answer(body: object) {
  return vi
    .fn()
    .mockResolvedValue(new Response(JSON.stringify(body), { status: 200 }));
}

describe("auto refresh on app open", () => {
  beforeEach(() => window.sessionStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("asks for a stale-only refresh and says when a run started", async () => {
    const fetchMock = answer({ started: true, runId: 7 });
    vi.stubGlobal("fetch", fetchMock);
    render(<AutoRefresh />);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Finding today's new roles",
    );
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/api\/runs\/refresh$/);
    expect(JSON.parse(init.body)).toEqual({ ifStale: true });
  });

  it("stays silent when nothing started", async () => {
    const fetchMock = answer({
      started: false,
      reason: "recommendations are fresh",
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AutoRefresh />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("checks once per session, not on every page", async () => {
    const fetchMock = answer({ started: false });
    vi.stubGlobal("fetch", fetchMock);
    const { unmount } = render(<AutoRefresh />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    unmount();
    render(<AutoRefresh />);
    // The second page only looks for a run in progress; it never asks for a refresh again.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(String(fetchMock.mock.calls[1][0])).toMatch(/\/api\/runs$/);
  });

  it("brings the banner back after a reload while a run is still going", async () => {
    window.sessionStorage.setItem("career-copilot:refresh-checked", "1");
    const fetchMock = answer([{ runId: 7, state: "running" }]);
    vi.stubGlobal("fetch", fetchMock);
    render(<AutoRefresh />);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Finding today's new roles",
    );
  });

  it("swallows an unreachable API", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("offline"));
    vi.stubGlobal("fetch", fetchMock);
    render(<AutoRefresh />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
