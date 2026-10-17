import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import SettingsPage from "@/app/settings/page";
import { Disclosure } from "@/components/disclosure";
import { needsApproval, type ModelRole, type PipelineRun } from "@/lib/api";

// The models panel has its own tests and endpoints; here it only holds its place.
vi.mock("@/components/model-settings", () => ({ ModelSettings: () => null }));

// The observability panel has its own tests and endpoints; here it only holds its place.
vi.mock("@/components/observability-section", () => ({
  ObservabilitySection: () => (
    <section className="run-card" aria-label="Observability">
      <Disclosure title="Observability"><p>panel</p></Disclosure>
    </section>
  ),
}));

function response(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body } as Response;
}

function role(name: string, kind: string, local: boolean): ModelRole {
  return {
    role: name, kind, model: "some-model", base_url: null, reachable: true,
    model_present: true, tools: true, detail: "", local,
  };
}

const LOCAL_ROLES = [role("triage", "ollama", true), role("match", "vllm", true)];
const PAID_ROLES = [role("triage", "ollama", true), role("match", "openrouter", false)];

function pastRun(runId: number, state: PipelineRun["state"], kind = "full"): PipelineRun {
  return {
    runId, state, kind, stage: "", completed: 3, total: 3, costEur: 0.42, budgetEur: 1,
    stalled: false, failureCategory: null, failureMessage: null, sourceFailures: [],
    startedAt: "2026-10-01T09:00:00+00:00", endedAt: "2026-10-01T09:05:00+00:00",
  };
}

type Health = ModelRole[] | "down";

/** Serves the calls the settings page makes on load; `extra` answers anything else. */
function serve(
  health: Health,
  runs: PipelineRun[] = [],
  extra?: (url: string, init?: RequestInit) => Response | undefined,
) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const answered = extra?.(url, init);
    if (answered) return answered;
    if (url.endsWith("/api/health/models")) {
      if (health === "down") return response({}, false, 500);
      return response({ ok: true, roles: health });
    }
    if (url.endsWith("/api/runs") && !init?.method) return response(runs);
    if (url.includes("/api/jobs?")) return response({ items: [], total: 0, page: 1, limit: 20 });
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** Lets the health answer land, so a card that would appear has appeared. */
async function settle(fetchMock: ReturnType<typeof serve>) {
  await waitFor(() => expect(fetchMock.mock.calls.some(
    ([input]) => String(input).endsWith("/api/health/models"),
  )).toBe(true));
  await act(async () => {});
}

describe("needsApproval", () => {
  it("is true only when some role is not local", () => {
    expect(needsApproval(LOCAL_ROLES)).toBe(false);
    expect(needsApproval(PAID_ROLES)).toBe(true);
    expect(needsApproval([])).toBe(false);
  });
});

describe("settings page", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("is titled Settings and marks only its own link as current", async () => {
    const fetchMock = serve(LOCAL_ROLES);
    render(<SettingsPage />);
    await settle(fetchMock);
    expect(screen.getByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    expect(screen.queryByText("Collect a new batch.")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "SETTINGS" })).toHaveAttribute("aria-current", "page");
    const nav = screen.getByRole("navigation", { name: "Main" });
    for (const link of within(nav).getAllByRole("link", { name: /PREFERENCES|RECOMMENDATIONS/ })) {
      expect(link).not.toHaveAttribute("aria-current");
    }
  });

  it("shows no spending approval when every role is local", async () => {
    const fetchMock = serve(LOCAL_ROLES);
    render(<SettingsPage />);
    await settle(fetchMock);
    expect(screen.queryByText("Your next run")).not.toBeInTheDocument();
    expect(screen.queryByText(/Approve up to/)).not.toBeInTheDocument();
    expect(screen.queryByText(/spending ceiling/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Preview run" })).not.toBeInTheDocument();
  });

  it("treats a failed health check as local and still shows the sections", async () => {
    const fetchMock = serve("down");
    render(<SettingsPage />);
    await settle(fetchMock);
    expect(screen.queryByText("Your next run")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run history" })).toBeInTheDocument();
  });

  it("keeps Observability, Results, Run history and the account sections closed, in that order", async () => {
    const fetchMock = serve(LOCAL_ROLES);
    render(<SettingsPage />);
    await settle(fetchMock);
    const toggles = screen.getAllByRole("button").filter((button) =>
      button.classList.contains("disclosure-toggle"),
    );
    expect(toggles.map((button) => button.textContent?.replace("›", "").trim())).toEqual([
      "Observability", "Results", "Run history", "Download your data", "Delete this account",
    ]);
    expect(toggles.map((button) => button.getAttribute("aria-expanded"))).toEqual([
      "false", "false", "false", "false", "false",
    ]);
    expect(screen.getByRole("columnheader", { name: "Cost", hidden: true })).toBeInTheDocument();
  });

  it("shows each history row without cost, tagged and worded with its state", async () => {
    const fetchMock = serve(LOCAL_ROLES, [pastRun(12, "succeeded"), pastRun(11, "failed")]);
    const { container } = render(<SettingsPage />);
    await settle(fetchMock);
    fireEvent.click(screen.getByRole("button", { name: "Run history" }));
    const rows = [...container.querySelectorAll(".run-history li")];
    expect(rows.map((row) => row.getAttribute("data-state"))).toEqual(["succeeded", "failed"]);
    expect(rows[0]).toHaveTextContent("Run 12 · full · succeeded");
    expect(rows[1]).toHaveTextContent("Run 11 · full · failed");
    for (const row of rows) expect(row.textContent).not.toContain("€");
  });

  it("with a paid role, previews the estimate, states the ceiling, and starts with its token", async () => {
    const fetchMock = serve(PAID_ROLES, [], (url, init) => {
      if (url.endsWith("/api/runs/preview")) {
        return response({
          candidatesNow: 12,
          estimatedCalls: 18,
          reservedEur: 1.44,
          ceilingEur: 1,
          mode: "incremental",
          token: "approved-config",
          modelBackendReady: true,
          modelBackendProblem: "",
        });
      }
      if (url.endsWith("/api/runs") && init?.method === "POST") {
        return response({ runId: 7, state: "running" });
      }
      return undefined;
    });
    render(<SettingsPage />);

    expect(await screen.findByRole("heading", { name: "Your next run" })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Preview run" }));
    expect(await screen.findByText("Approve up to €1.00")).toBeInTheDocument();
    expect(screen.getByText(/The run stops at €1.00/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approve €1.00 and run" }));
    expect(await screen.findByRole("heading", { name: "Run 7 · running" })).toBeInTheDocument();

    const start = fetchMock.mock.calls.find(
      ([input, init]) => String(input).endsWith("/api/runs") && init?.method === "POST",
    );
    expect(JSON.parse(start?.[1]?.body as string)).toEqual({
      token: "approved-config",
      ceilingEur: 1,
    });
  });

  it("says a key is missing instead of offering an approval it cannot honour", async () => {
    serve(
      [
        role("triage", "ollama", true),
        {
          ...role("match", "openrouter", false),
          reachable: false,
          model_present: false,
          detail: "no OpenRouter key (OPENROUTER_API_KEY or Settings)",
        },
      ],
      [], (url) => {
      if (url.endsWith("/api/runs/preview")) {
        return response({
          candidatesNow: 64,
          estimatedCalls: 18,
          reservedEur: 3.6,
          ceilingEur: 1,
          mode: "incremental",
          token: "t",
          modelBackendReady: false,
          modelBackendProblem: "triage → openrouter:x (no OpenRouter key)",
          modelBackendKeyMissing: true,
        });
      }
      return undefined;
    });
    render(<SettingsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Preview run" }));
    expect(await screen.findByText("Can’t run yet")).toBeInTheDocument();
    expect(screen.getByText(/Add an OpenRouter key to run in the cloud/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
    // The roles sit in a block of boxes, like Agent runs, behind "Model calls".
    fireEvent.click(screen.getByRole("button", { name: /Model calls/ }));
    const flow = screen.getByRole("list", { name: "Model roles" });
    expect(within(flow).getAllByTestId("flow-box")).toHaveLength(2);
    expect(within(flow).getByText("no key")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Models settings" })).toHaveAttribute(
      "href",
      "#model-setup",
    );

    // Cancel goes back to how the card was before Preview run.
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText("Can’t run yet")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preview run" })).toBeEnabled();
  });

  it("keeps deterministic outcomes visually separate and requests server-side sorting", async () => {
    const fetchMock = serve(LOCAL_ROLES, [], (url) => {
      if (!url.includes("/api/jobs?")) return undefined;
      return response({
        items: [{
          id: 1, title: "Platform Engineer", company: "Acme", location: "Berlin",
          source: "greenhouse", url: "https://example.test/1",
          firstSeenAt: "2026-09-06T00:00:00+00:00", outcome: "pass",
          triageScore: 82, matchScore: 91, costEur: 0.021,
        }],
        total: 1, page: 1, limit: 20,
      });
    });
    render(<SettingsPage />);

    fireEvent.click(screen.getByRole("button", { name: "Results" }));
    expect(await screen.findByRole("link", { name: "Platform Engineer" })).toBeInTheDocument();
    expect(screen.getByText("pass")).toHaveClass("outcome-pass");
    fireEvent.click(screen.getByRole("button", { name: "Company" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(
      ([input]) => String(input).includes("sort=company&direction=asc"),
    )).toBe(true));
  });
});
