import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RefreshJobs } from "@/components/refresh-jobs";

function response(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body } as Response;
}

const PREVIEW = {
  candidatesNow: 12,
  estimatedCalls: 24,
  reservedEur: 0.4,
  ceilingEur: 0.5,
  mode: "cloud",
  token: "tok",
  modelBackendReady: true,
  modelBackendProblem: "",
};

function stubRuns(
  refresh: unknown,
  states: string[] = ["succeeded"],
  history: unknown[] = [],
) {
  const remaining = [...states];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/api/runs")) return response(history);
    if (url.endsWith("/api/runs/refresh")) return response(refresh);
    if (/\/api\/runs\/\d+$/.test(url)) {
      return response({ id: 9, state: remaining.shift() ?? "succeeded" });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("look for new jobs", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("searches, refetches the list, and counts only ids not seen before", async () => {
    stubRuns({ started: true, runId: 9 }, ["running", "succeeded"]);
    const onRefetch = vi.fn(async () => [1, 2, 3]);
    render(<RefreshJobs knownIds={[1]} onRefetch={onRefetch} pollMs={1} />);

    fireEvent.click(screen.getByRole("button", { name: "Look for new jobs" }));
    expect(await screen.findByText("Searching…")).toBeInTheDocument();
    expect(await screen.findByText("Found 2 new")).toBeInTheDocument();
    expect(onRefetch).toHaveBeenCalledTimes(1);
  });

  it("says nothing new was found when every id was already listed", async () => {
    stubRuns({ started: true, runId: 9 });
    const onRefetch = vi.fn(async () => [1]);
    render(<RefreshJobs knownIds={[1]} onRefetch={onRefetch} pollMs={1} />);

    fireEvent.click(screen.getByRole("button", { name: "Look for new jobs" }));
    expect(await screen.findByText("Nothing new found")).toBeInTheDocument();
  });

  it("picks a run back up after a reload instead of showing an idle button", async () => {
    stubRuns({ started: false }, ["running", "succeeded"], [{ runId: 9, state: "running" }]);
    const onRefetch = vi.fn(async () => [1]);
    render(<RefreshJobs knownIds={[]} onRefetch={onRefetch} pollMs={1} />);

    expect(await screen.findByText("Searching…")).toBeInTheDocument();
    expect(await screen.findByText("Search finished")).toBeInTheDocument();
    expect(onRefetch).toHaveBeenCalledTimes(1);
  });

  it("shows the server's reason when no run started", async () => {
    stubRuns({ started: false, reason: "upload a CV first" });
    const onRefetch = vi.fn(async () => []);
    render(<RefreshJobs knownIds={[]} onRefetch={onRefetch} pollMs={1} />);

    fireEvent.click(screen.getByRole("button", { name: "Look for new jobs" }));
    expect(await screen.findByText("upload a CV first")).toBeInTheDocument();
    expect(onRefetch).not.toHaveBeenCalled();
  });

  describe("with a paid model", () => {
    const billed = { started: false, code: "billed_roles", reason: "local only" };

    function stubCloud(show: boolean) {
      const posts: { url: string; body: unknown }[] = [];
      const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/runs/refresh")) return response(billed);
        if (url.endsWith("/api/runs/preview")) return response(PREVIEW);
        if (url.endsWith("/api/runs/cloud-notice")) {
          if (init?.method === "POST") {
            posts.push({ url, body: JSON.parse(String(init.body)) });
            return response({ show: false });
          }
          return response({ show, redactionEnabled: true, providers: ["openrouter"] });
        }
        if (url.endsWith("/api/runs")) {
          posts.push({ url, body: JSON.parse(String(init?.body)) });
          return response({ runId: 9, state: "running" });
        }
        if (/\/api\/runs\/\d+$/.test(url)) return response({ id: 9, state: "succeeded" });
        throw new Error(`unexpected request: ${url}`);
      });
      vi.stubGlobal("fetch", fetchMock);
      return posts;
    }

    it("shows the redaction notice and cost, then runs once approved", async () => {
      const posts = stubCloud(true);
      const onRefetch = vi.fn(async () => [1, 2]);
      render(<RefreshJobs knownIds={[1]} onRefetch={onRefetch} pollMs={1} />);

      fireEvent.click(screen.getByRole("button", { name: "Look for new jobs" }));
      expect(await screen.findByText(/sent to OpenRouter/)).toBeInTheDocument();
      expect(screen.getByLabelText("Remind me next time")).not.toBeChecked();
      fireEvent.click(screen.getByRole("button", { name: /Approve €0.50 and run/ }));

      expect(await screen.findByText("Found 1 new")).toBeInTheDocument();
      expect(posts).toEqual([
        { url: expect.stringContaining("cloud-notice"), body: { remind: false } },
        { url: expect.stringMatching(/\/api\/runs$/), body: { token: "tok", ceilingEur: 0.5 } },
      ]);
    });

    it("keeps the notice when the reminder box is checked", async () => {
      const posts = stubCloud(true);
      render(<RefreshJobs knownIds={[]} onRefetch={async () => []} pollMs={1} />);

      fireEvent.click(screen.getByRole("button", { name: "Look for new jobs" }));
      fireEvent.click(await screen.findByLabelText("Remind me next time"));
      fireEvent.click(screen.getByRole("button", { name: /Approve/ }));

      expect(await screen.findByText("Nothing new found")).toBeInTheDocument();
      expect(posts[0].body).toEqual({ remind: true });
    });

    it("skips the notice once dismissed and still asks for approval", async () => {
      const posts = stubCloud(false);
      render(<RefreshJobs knownIds={[]} onRefetch={async () => []} pollMs={1} />);

      fireEvent.click(screen.getByRole("button", { name: "Look for new jobs" }));
      fireEvent.click(await screen.findByRole("button", { name: /Approve/ }));

      expect(screen.queryByText(/sent to OpenRouter/)).not.toBeInTheDocument();
      expect(await screen.findByText("Nothing new found")).toBeInTheDocument();
      expect(posts).toHaveLength(1);
    });
  });
});
