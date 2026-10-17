import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import RecycleBinPage from "@/app/recycle-bin/page";
import type { BinItem } from "@/lib/api";

function respond(status: number, body?: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

type Handler = () => ReturnType<typeof respond>;

/** Routes each request by method and path, so call order does not matter. */
function serve(routes: Record<string, Handler>) {
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const path = new URL(input, "http://local").pathname;
    const handler = routes[`${init?.method ?? "GET"} ${path}`];
    if (!handler) throw new Error(`unexpected request ${init?.method ?? "GET"} ${path}`);
    return handler();
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const daysAgo = (n: number) => new Date(Date.now() - n * 86_400_000).toISOString();

function item(overrides: Partial<BinItem> = {}): BinItem {
  return {
    jobId: 7,
    title: "Data Engineer",
    company: "Acme GmbH",
    dismissedAt: daysAgo(2),
    daysLeft: 5,
    ...overrides,
  };
}

const calls = (fetchMock: ReturnType<typeof serve>, key: string) =>
  fetchMock.mock.calls.filter(
    ([input, init]) =>
      `${init?.method ?? "GET"} ${new URL(input, "http://local").pathname}` === key,
  );

describe("recycle bin", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("shows each item with how long ago it was dismissed and the days left", async () => {
    serve({ "GET /api/bin": () => respond(200, { days: 7, items: [item()] }) });
    render(<RecycleBinPage />);
    expect(await screen.findByText("Data Engineer")).toBeInTheDocument();
    expect(screen.getByText("Acme GmbH")).toBeInTheDocument();
    expect(screen.getByText("Dismissed 2 days ago")).toBeInTheDocument();
    expect(screen.getByText("5 days left")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Recommendations/ })).toHaveAttribute(
      "href",
      "/recommendations",
    );
  });

  it("uses the singular for one day", async () => {
    serve({
      "GET /api/bin": () =>
        respond(200, { days: 7, items: [item({ dismissedAt: daysAgo(1), daysLeft: 1 })] }),
    });
    render(<RecycleBinPage />);
    expect(await screen.findByText("Dismissed 1 day ago")).toBeInTheDocument();
    expect(screen.getByText("1 day left")).toBeInTheDocument();
  });

  it("says Nothing here when the bin is empty", async () => {
    serve({ "GET /api/bin": () => respond(200, { days: 7, items: [] }) });
    render(<RecycleBinPage />);
    expect(await screen.findByText("Nothing here")).toBeInTheDocument();
  });

  it("restore removes the row", async () => {
    const fetchMock = serve({
      "GET /api/bin": () => respond(200, { days: 7, items: [item()] }),
      "POST /api/jobs/7/restore": () => respond(200, { jobId: 7, decision: null }),
    });
    render(<RecycleBinPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    await waitFor(() => expect(screen.queryByText("Data Engineer")).not.toBeInTheDocument());
    expect(calls(fetchMock, "POST /api/jobs/7/restore")).toHaveLength(1);
  });

  it("delete forever asks first, and No does nothing", async () => {
    const fetchMock = serve({
      "GET /api/bin": () => respond(200, { days: 7, items: [item()] }),
      "DELETE /api/bin/7": () => respond(204),
    });
    render(<RecycleBinPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete forever" }));
    expect(screen.getByText("Delete forever? This cannot be undone.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "No" }));
    expect(screen.queryByText("Delete forever? This cannot be undone.")).not.toBeInTheDocument();
    expect(screen.getByText("Data Engineer")).toBeInTheDocument();
    expect(calls(fetchMock, "DELETE /api/bin/7")).toHaveLength(0);
  });

  it("delete forever with Yes removes the row", async () => {
    const fetchMock = serve({
      "GET /api/bin": () => respond(200, { days: 7, items: [item()] }),
      "DELETE /api/bin/7": () => respond(204),
    });
    render(<RecycleBinPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete forever" }));
    fireEvent.click(screen.getByRole("button", { name: "Yes" }));
    await waitFor(() => expect(screen.queryByText("Data Engineer")).not.toBeInTheDocument());
    expect(calls(fetchMock, "DELETE /api/bin/7")).toHaveLength(1);
  });
});
