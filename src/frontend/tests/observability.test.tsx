import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ObservabilitySection } from "@/components/observability-section";
import { AGENT_TIP, DONE_TIP, toolTip } from "@/lib/flow-glossary";
import { formatSeconds, slowLevel } from "@/lib/format-seconds";
import { linePaths } from "@/lib/observability-chart";

function response(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body } as Response;
}

const SUMMARY = {
  days: 30,
  tiles: { tokensPerS: 42.86, avgLatencyS: 1.75, totalTokens: 600, calls: 4, errorRate: 0.25 },
  rows: [
    {
      model: "qwen3:4b-instruct",
      backend: "ollama",
      stage: "match",
      calls: 4,
      tokensIn: 300,
      tokensOut: 300,
      tokensPerSP50: 50,
      latencyP50: 2,
      latencyP95: 2.4,
    },
  ],
};
const TOOLS = [
  { agent: "curator", tool: "get_job", calls: 4, slowestS: 0.0452, refused: 1, errors: 0 },
  { agent: "curator", tool: "search_pool", calls: 2, slowestS: 0.00002, refused: 0, errors: 1 },
];
const FLOWS = [
  {
    agent: "curator",
    model: "qwen3:4b-instruct",
    outcome: "ok",
    requests: 4,
    tokensIn: 900,
    tokensOut: 60,
    startedAt: "2026-10-08T10:00:00+00:00",
    totalS: 9.4,
    steps: [
      { tool: "search_pool", outcome: "ok", toolS: 0.0452, stepS: 2.13 },
      { tool: "get_job", outcome: "ok", toolS: 0.00002, stepS: 3.2 },
      { tool: "shortlist", outcome: "refused", toolS: 0.00001, stepS: 1.4 },
    ],
    endS: 2.67,
  },
];
const SERIES = [
  { day: "2026-10-06", model: "qwen3:4b-instruct", tokensPerS: 40 },
  { day: "2026-10-07", model: "qwen3:4b-instruct", tokensPerS: 45 },
  { day: "2026-10-07", model: "gemini", tokensPerS: 120 },
];
const SESSIONS = [
  {
    id: 2,
    startedAt: "2026-10-08T10:00:00+00:00",
    endedAt: "2026-10-08T11:00:00+00:00",
    durationS: 3600,
    outcome: "clean",
    profile: "laptop",
    logPath: "data/logs/app-20261008T100000.log",
    hasLog: true,
  },
  {
    id: 1,
    startedAt: "2026-10-07T10:00:00+00:00",
    endedAt: null,
    durationS: null,
    outcome: "unclean",
    profile: null,
    logPath: "data/logs/app-20261007T100000.log",
    hasLog: false,
  },
];

function stub(empty = false, series = SERIES) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/observability/summary")) {
      return response(
        empty
          ? { days: 30, tiles: { ...SUMMARY.tiles, calls: 0, tokensPerS: 0, totalTokens: 0 }, rows: [] }
          : SUMMARY,
      );
    }
    if (url.includes("/api/observability/cost")) {
      return response({
        days: 7,
        totalEur: 0.4,
        byBackend: [{ backend: "openrouter", eur: 0.4, calls: 3 }],
        byModel: [
          { model: "R", backend: "openrouter", stage: "match", eur: 0.4, calls: 3 },
        ],
        byDay: [{ day: "2026-10-08", eur: 0.4 }],
      });
    }
    if (url.includes("/api/observability/tools")) return response(empty ? [] : TOOLS);
    if (url.includes("/api/observability/flows")) return response(empty ? [] : FLOWS);
    if (url.includes("/api/observability/series")) return response(empty ? [] : series);
    if (url.includes("/api/observability/sessions")) return response(empty ? [] : SESSIONS);
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function openSection() {
  render(<ObservabilitySection />);
  fireEvent.click(screen.getByRole("button", { name: /Observability/ }));
}

function openGroup(name: string | RegExp) {
  fireEvent.click(screen.getByRole("button", { name }));
}

describe("observability section", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("renders the tiles from the summary", async () => {
    stub();
    openSection();
    expect(await screen.findByText("42.86")).toBeInTheDocument();
    expect(screen.getByText("Output tokens/s")).toBeInTheDocument();
    expect(screen.getByText("1.8 s")).toBeInTheDocument();
    expect(screen.getByText("600")).toBeInTheDocument();
    expect(screen.getByText("25%")).toBeInTheDocument();
  });

  it("shows what the window cost and where it went", async () => {
    stub();
    openSection();
    expect(await screen.findByText("Spent")).toBeInTheDocument();
    expect(screen.getAllByText("€0.40").length).toBeGreaterThan(0);
    openGroup("Cost");
    expect(
      screen.getByRole("table", { name: "Cost per model and stage" }),
    ).toBeInTheDocument();
  });

  it("is closed by default and fetches nothing until opened", async () => {
    const fetchMock = stub();
    render(<ObservabilitySection />);
    const toggle = screen.getByRole("button", { name: /Observability/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText("42.86")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalled();
  });

  it("keeps every group closed inside the opened section", async () => {
    stub();
    openSection();
    await screen.findByText("42.86");
    for (const name of ["Models", "Tool totals", "Speed over time", "Sessions"]) {
      expect(screen.getByRole("button", { name: new RegExp(`^›?\\s*${name}$`) })).toHaveAttribute(
        "aria-expanded",
        "false",
      );
    }
  });

  it("lists the model table, and keeps tool totals behind a dropdown", async () => {
    stub();
    openSection();
    await screen.findByText("42.86");
    openGroup(/^›?\s*Models$/);
    const models = within(screen.getByRole("table", { name: "Calls per model and stage" }));
    expect(models.getByText("qwen3:4b-instruct")).toBeInTheDocument();
    expect(models.getByText("match")).toBeInTheDocument();
    expect(models.getByText("50")).toBeInTheDocument();

    const toggle = screen.getByRole("button", { name: /^›?\s*Tool totals$/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    const tools = within(screen.getByRole("table", { name: "Calls per tool" }));
    expect(tools.getByText("get_job")).toBeInTheDocument();
    expect(tools.getByText("45 ms")).toBeInTheDocument();
    expect(tools.getByText("<1 ms")).toBeInTheDocument();
    expect(tools.queryByText(/p95/i)).not.toBeInTheDocument();
    expect(tools.queryByText(/avg/i)).not.toBeInTheDocument();
  });

  const BOXES = (count: number) =>
    Array.from({ length: count }, (_, index) => ({
      ...FLOWS[0],
      startedAt: `2026-10-08T1${index}:00:00+00:00`,
    }));

  async function openRuns() {
    openSection();
    fireEvent.click(screen.getByRole("button", { name: "Today" }));
    await screen.findByRole("button", { name: /^›?\s*Agent runs$/ });
    openGroup(/^›?\s*Agent runs$/);
  }

  it("draws each agent run as boxes joined by arrows, with the time on each arrow", async () => {
    stub();
    await openRuns();
    const run = within(await screen.findByRole("list", { name: "Agent runs" }));
    expect(run.getByText(/4 requests/)).toBeInTheDocument();
    const boxes = run.getAllByTestId("flow-box").map((box) => box.textContent);
    expect(boxes[0]).toMatch(/curator/);
    expect(boxes[1]).toMatch(/search_pool/);
    expect(boxes[3]).toMatch(/shortlist.*refused/);
    expect(boxes[boxes.length - 1]).toMatch(/done · ok/);
    const times = run.getAllByTestId("flow-time").map((t) => t.textContent);
    expect(times).toEqual(["2.1 s", "3.2 s", "1.4 s", "2.7 s"]);
    expect(run.getAllByText("45 ms")).toHaveLength(1);
  });

  it("marks slow steps by level and leaves fast ones plain", async () => {
    stub();
    await openRuns();
    const run = within(await screen.findByRole("list", { name: "Agent runs" }));
    const levels = run.getAllByTestId("flow-time").map((t) => t.getAttribute("data-slow"));
    expect(levels).toEqual(["0", "1", "0", "0"]);
  });

  it("shows one colour key with the box roles and the step scale, and no under-3-seconds entry", async () => {
    stub();
    await openRuns();
    const key = within(screen.getByRole("group", { name: "Colour key" }));
    for (const label of ["agent", "tool", "done", "refused", "error", "3–5 s", "5–8 s", "8–10 s", "10 s+"]) {
      expect(key.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByRole("group", { name: "Colour key" }).textContent).not.toMatch(/under 3|<\s*3/i);
  });

  it("explains every box behind an info button", async () => {
    stub();
    await openRuns();
    fireEvent.click(screen.getByRole("button", { name: "About search_pool" }));
    expect(screen.getByRole("note")).toHaveTextContent(toolTip("search_pool"));
    fireEvent.pointerDown(document.body);
    fireEvent.click(screen.getByRole("button", { name: "About done" }));
    expect(screen.getByRole("note")).toHaveTextContent(DONE_TIP("ok"));
    fireEvent.pointerDown(document.body);
    fireEvent.click(screen.getByRole("button", { name: "About curator agent" }));
    expect(screen.getByRole("note")).toHaveTextContent(AGENT_TIP("curator"));
  });

  it("asks for the last three runs, shows the latest and nests the two before it", async () => {
    const fetchMock = stub();
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/flows")) return response(BOXES(3));
      if (url.includes("/summary")) return response(SUMMARY);
      if (url.includes("/tools")) return response(TOOLS);
      if (url.includes("/series")) return response(SERIES);
      if (url.includes("/cost")) throw new Error("no cost");
      return response(SESSIONS);
    });
    await openRuns();
    expect(
      fetchMock.mock.calls.some(([u]) => String(u).includes("flows?days=1&limit=3")),
    ).toBe(true);
    const latest = within(screen.getByRole("list", { name: "Agent runs" }));
    expect(latest.getAllByTestId("flow-box").length).toBe(5);
    const earlier = screen.getByRole("button", { name: /^›?\s*Earlier runs \(2\)$/ });
    expect(earlier).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(earlier);
    expect(screen.getByRole("list", { name: "Earlier agent runs" })).toBeInTheDocument();
    const older = within(screen.getByRole("list", { name: "Earlier agent runs" }));
    expect(older.getAllByTestId("flow-box").length).toBe(10);
    expect(screen.getByText(/Older runs are in the session log/)).toBeInTheDocument();
  });

  it("shows agent runs for Today only, never under a wider window", async () => {
    const fetchMock = stub();
    openSection();
    await screen.findByText("42.86");
    expect(screen.queryByRole("button", { name: /^›?\s*Agent runs$/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Today" }));
    expect(await screen.findByRole("button", { name: /^›?\s*Agent runs$/ })).toBeInTheDocument();

    for (const label of ["7 days", "30 days", "90 days"]) {
      fireEvent.click(screen.getByRole("button", { name: label }));
      await waitFor(() =>
        expect(screen.queryByRole("button", { name: /^›?\s*Agent runs$/ })).not.toBeInTheDocument(),
      );
    }
    const flowCalls = fetchMock.mock.calls.filter(([u]) => String(u).includes("/flows"));
    expect(flowCalls.map(([u]) => String(u).split("?")[1])).toEqual(["days=1&limit=3"]);
  });

  it("explains each tile in an info popover that closes on an outside click", async () => {
    stub();
    openSection();
    await screen.findByText("42.86");
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "About Avg latency" }));
    expect(screen.getByRole("note")).toHaveTextContent(/one model request/i);
    expect(screen.getByRole("note")).toHaveTextContent(/not per job/i);
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    for (const label of ["Output tokens/s", "Total tokens", "Calls", "Error rate"]) {
      expect(screen.getByRole("button", { name: `About ${label}` })).toBeInTheDocument();
    }
  });

  it("draws one line per model as inline SVG", async () => {
    stub();
    openSection();
    await screen.findByText("42.86");
    openGroup(/^›?\s*Speed over time$/);
    const chart = await screen.findByRole("img", { name: /Daily tokens per second/ });
    expect(chart.tagName.toLowerCase()).toBe("svg");
    expect(chart.querySelectorAll("polyline")).toHaveLength(2);
    expect(within(chart.parentElement as HTMLElement).getByText("gemini")).toBeInTheDocument();
  });

  it("lists recent sessions with duration, outcome, profile and log path", async () => {
    stub();
    openSection();
    await screen.findByText("42.86");
    openGroup(/^›?\s*Sessions$/);
    const row = (await screen.findByText("app-20261008T100000.log")).closest("li");
    expect(within(row as HTMLElement).getByText(/clean/)).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText(/1h 0m/)).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText(/laptop/)).toBeInTheDocument();
  });

  it("offers a download for sessions whose log still exists", async () => {
    stub();
    openSection();
    await screen.findByText("42.86");
    openGroup(/^›?\s*Sessions$/);
    const link = await screen.findByRole("link", { name: /Download/ });
    expect(link.getAttribute("href")).toMatch(/\/api\/observability\/sessions\/2\/log$/);
    expect(link).toHaveAttribute("download");
    expect(screen.getAllByRole("link", { name: /Download/ })).toHaveLength(1);
    const gone = (await screen.findByText("app-20261007T100000.log")).closest("li");
    expect(within(gone as HTMLElement).getByText(/unclean/)).toBeInTheDocument();
  });

  it("defaults to 7 days, offers today, and switches the window", async () => {
    const fetchMock = stub();
    openSection();
    await screen.findByText("42.86");
    const asked = (tail: string) =>
      fetchMock.mock.calls.some(([u]) => String(u).includes(tail));
    expect(asked("summary?days=7")).toBe(true);
    expect(asked("summary?days=30")).toBe(false);
    expect(asked("flows?days=7")).toBe(false);
    expect(screen.getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Today" }));
    await screen.findByText("42.86");
    expect(asked("summary?days=1")).toBe(true);
    expect(asked("flows?days=1&limit=3")).toBe(true);
    expect(screen.getByRole("button", { name: "Today" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "30 days" }));
    await screen.findByText("42.86");
    expect(asked("series?days=30")).toBe(true);
  });

  it("draws no chart from a single day, and says when the trend will appear", async () => {
    stub(false, [{ day: "2026-10-07", model: "qwen3:4b-instruct", tokensPerS: 45 }]);
    openSection();
    await screen.findByText("42.86");
    openGroup(/^›?\s*Speed over time$/);
    expect(screen.queryByRole("img", { name: /Daily tokens per second/ })).not.toBeInTheDocument();
    expect(screen.getByText(/Trend appears after two days of data/)).toBeInTheDocument();
  });

  it("says so when nothing has been recorded", async () => {
    stub(true);
    openSection();
    expect(await screen.findByText("No calls recorded yet")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows an error when the service is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new Error("down"))));
    openSection();
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not reach/);
  });
});

describe("linePaths", () => {
  it("groups by model, orders days, and scales to the box", () => {
    const lines = linePaths(SERIES, { width: 100, height: 50, pad: 0 });
    expect(lines.map((l) => l.model).sort()).toEqual(["gemini", "qwen3:4b-instruct"]);
    const qwen = lines.find((l) => l.model === "qwen3:4b-instruct")!;
    // Two days across the full width; 120 tokens/s is the top, 40 sits a third of the way up.
    expect(qwen.points).toEqual([
      { x: 0, y: 50 - (40 / 120) * 50, day: "2026-10-06", value: 40 },
      { x: 100, y: 50 - (45 / 120) * 50, day: "2026-10-07", value: 45 },
    ]);
  });

  it("returns nothing for no data", () => {
    expect(linePaths([], { width: 100, height: 50, pad: 0 })).toEqual([]);
  });

  it("puts a single-day line in the middle rather than dividing by zero", () => {
    const [line] = linePaths([{ day: "2026-10-07", model: "a", tokensPerS: 10 }], {
      width: 100,
      height: 50,
      pad: 0,
    });
    expect(line.points[0].x).toBe(50);
  });
});

describe("formatSeconds", () => {
  it("never rounds a fast call down to zero", () => {
    expect(formatSeconds(0.00002)).toBe("<1 ms");
    expect(formatSeconds(0.0452)).toBe("45 ms");
    expect(formatSeconds(1.75)).toBe("1.8 s");
    expect(formatSeconds(2)).toBe("2 s");
    expect(formatSeconds(null)).toBe("—");
  });
});

describe("slowLevel", () => {
  it("steps up at 3, 5, 8 and 10 seconds, taking the higher level on the edge", () => {
    const levels = [0, 2.9, 3, 4.9, 5, 7.9, 8, 9.9, 10, 42].map(slowLevel);
    expect(levels).toEqual([0, 0, 1, 1, 2, 2, 3, 3, 4, 4]);
    expect(slowLevel(null)).toBe(0);
  });
});

describe("flow glossary", () => {
  it("explains known tools and falls back to the tool's name", () => {
    expect(toolTip("search_pool")).toMatch(/searches the scored job pool/);
    expect(toolTip("brand_new_tool")).toBe("Tool: brand_new_tool");
    expect(AGENT_TIP("curator")).toMatch(/curator/);
    expect(DONE_TIP("error")).toMatch(/"error"/);
  });
});
