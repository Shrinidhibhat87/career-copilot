import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AssistantChat } from "@/components/assistant-chat";
import { describeRun, parseRunCounts } from "@/lib/run-watch";
import type { PipelineRun } from "@/lib/api";

function response(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body } as Response;
}

const SUMMARY =
  "discover → score → digest | candidates=40 matched=9 above=6/6 capped=3";

function run(overrides: Partial<PipelineRun> = {}): PipelineRun {
  return {
    runId: 7,
    state: "succeeded",
    kind: "full",
    stage: "digest",
    completed: 1,
    total: 1,
    costEur: 0,
    budgetEur: 0,
    stalled: false,
    failureCategory: null,
    failureMessage: null,
    sourceFailures: [],
    startedAt: "2026-10-09T10:00:00Z",
    endedAt: "2026-10-09T10:01:00Z",
    summary: SUMMARY,
    ...overrides,
  };
}

/** One chat message whose reply carries `actions`; run polls answer from `polls`. */
function stub(actions: unknown[], polls: PipelineRun[] = [run()]) {
  const remaining = [...polls];
  const notes: string[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/assistant/note")) {
      notes.push(JSON.parse(String(init?.body)).text);
      return response({ saved: true }, true, 201);
    }
    if (url.endsWith("/api/assistant")) {
      return response({ text: "Started a search.", actions, busy: false });
    }
    if (url.endsWith("/api/runs")) return response([run({ state: "running" })]);
    if (/\/api\/runs\/\d+$/.test(url)) return response(remaining.shift() ?? polls[0]);
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { notes };
}

async function ask(onRefetch = vi.fn(async () => [1, 2, 3]), pollMs = 1) {
  const onActions = vi.fn();
  render(
    <AssistantChat
      knownIds={[1]}
      onActions={onActions}
      onRefetch={onRefetch}
      pollMs={pollMs}
      view={{ limit: 10, includeBelow: false }}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Open Copilot chat" }));
  const box = screen.getByLabelText("Message to the assistant");
  fireEvent.change(box, { target: { value: "find FDE roles" } });
  fireEvent.keyDown(box, { key: "Enter" });
  return { onRefetch, onActions };
}

describe("a search started from the chat", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("shows progress, then says what it found, reloads the list and saves the note", async () => {
    const { notes } = stub(
      [{ kind: "refresh", started: true, runId: 7 }],
      [
        ...Array.from({ length: 3 }, () =>
          run({ state: "running", stage: "score", completed: 2, total: 9 }),
        ),
        run(),
      ],
    );
    const { onRefetch } = await ask(undefined, 40);

    expect(await screen.findByText(/Searching: score 2\/9/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Details" })).toHaveAttribute(
      "target",
      "_blank",
    );
    expect(
      await screen.findByText(/Found 2 new roles\. 9 roles scored, 6 cleared/),
    ).toBeInTheDocument();
    expect(onRefetch).toHaveBeenCalledTimes(1);
    expect(notes).toHaveLength(1);
    expect(notes[0]).toContain("Found 2 new roles");
  });

  it("says nothing new was found", async () => {
    stub([{ kind: "refresh", started: true, runId: 7 }], [run({ summary: null })]);
    await ask(vi.fn(async () => [1]));
    expect(await screen.findByText("Nothing new found.")).toBeInTheDocument();
  });

  it("reports a failed run with its message and does not reload the list", async () => {
    stub(
      [{ kind: "refresh", started: true, runId: 7 }],
      [run({ state: "failed", failureMessage: "Ollama is not reachable" })],
    );
    const { onRefetch } = await ask();
    expect(
      await screen.findByText("The search failed: Ollama is not reachable"),
    ).toBeInTheDocument();
    expect(onRefetch).not.toHaveBeenCalled();
  });

  it("says why no search started, and follows the run already active when queued", async () => {
    stub([
      { kind: "refresh", started: false, reason: "queued after the active run" },
    ]);
    await ask();
    expect(
      await screen.findByText("No search started: queued after the active run."),
    ).toBeInTheDocument();
    expect(await screen.findByText(/Found 2 new roles/)).toBeInTheDocument();
  });
});

describe("describing a run", () => {
  it("reads the counts a run summary carries", () => {
    expect(parseRunCounts(SUMMARY)).toEqual({
      candidates: 40,
      matched: 9,
      above: 6,
      target: 6,
      capped: 3,
    });
    expect(parseRunCounts("discover → score → digest")).toBeNull();
  });

  it("names the limit that stopped the run", () => {
    const text = describeRun(run(), 2);
    expect(text).toContain("stopped at your limit of 6 per search");
    expect(text).toContain("3 roles held back by the per-company limit");
  });

  it("stays quiet about limits that did not apply", () => {
    const text = describeRun(
      run({ summary: "x | candidates=5 matched=2 above=1/6 capped=0" }),
      1,
    );
    expect(text).not.toContain("limit");
  });
});

describe("the input after a reply", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("stays usable for a second message when the reply did no action", async () => {
    stub([]);
    await ask();
    expect(await screen.findByText("Started a search.")).toBeInTheDocument();
    const box = screen.getByLabelText("Message to the assistant");
    expect(box).toBeEnabled();
    fireEvent.change(box, { target: { value: "and one more" } });
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  });
});
