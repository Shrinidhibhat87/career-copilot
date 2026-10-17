import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ReviewPage from "@/app/recommendations/page";
import type { ReviewPage as ReviewPageData, ReviewRow } from "@/lib/api";

function response(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body } as Response;
}

function row(id: number, title: string): ReviewRow {
  return {
    id,
    title,
    company: "Example GmbH",
    companyUrl: null,
    location: "Berlin",
    source: "greenhouse",
    url: `https://example.test/${id}`,
    firstSeenAt: new Date().toISOString(),
    lastSeenAt: new Date().toISOString(),
    outcome: "pass",
    score: 82,
    scoreStage: "match",
    verdict: "strong",
    why: "Fits.",
    label: null,
    labelStale: false,
    belowThreshold: false,
    locationMismatch: false,
    decision: null,
    isSaved: false,
    isApplied: false,
    notionIndex: null,
    isNew: false,
    viewedAt: null,
    scoredWithCv: null,
  };
}

function page(items: ReviewRow[], binCount = 0): ReviewPageData {
  return {
    items,
    inactiveAfterDays: 21,
    threshold: 30,
    limit: 10,
    kept: items.length,
    dropped: 0,
    below: 0,
    unscored: 0,
    total: items.length,
    binCount,
  };
}

const ROWS = [row(1, "Senior AI Engineer"), row(2, "Data Analyst")];

/** Answers the list from `lists` (the last one repeats), the chat and the review POST. */
function server(lists: ReviewPageData[], actions: unknown[] = []) {
  const remaining = [...lists];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/assistant")) {
        return response({ text: "Done.", actions, busy: false });
      }
      if (url.includes("/api/reviews") && init?.method === "POST") {
        return response({ reviewId: 1, label: "not_relevant", decision: "dismissed" }, true, 201);
      }
      if (url.includes("/api/review")) {
        return response(remaining.length > 1 ? remaining.shift() : remaining[0]);
      }
      throw new Error(`unexpected request: ${url}`);
    }),
  );
}

function reduceMotion(reduce: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({ matches: reduce && query.includes("reduce") })),
  );
}

describe("recycle bin link", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("keeps its name, with the icon hidden from assistive tech", async () => {
    server([page(ROWS, 3)]);
    render(<ReviewPage />);
    await screen.findByRole("button", { name: "Select: Senior AI Engineer" });

    const link = screen.getByRole("link", { name: "Recycle bin (3)" });
    expect(link).toHaveClass("bin-link");
    expect(link.querySelector('span[aria-hidden="true"]')).not.toBeNull();
  });
});

describe("a dismissed row flying to the bin", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  async function dismiss() {
    render(<ReviewPage />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Not relevant: Senior AI Engineer",
      }),
    );
  }

  it("animates the row away, then drops it, and counts it in the bin", async () => {
    reduceMotion(false);
    server([page(ROWS, 3)]);
    await dismiss();

    const ghost = await screen.findByTestId("leaving-row");
    expect(ghost).toHaveClass("row-leaving");
    expect(ghost.style.getPropertyValue("--bin-dx")).not.toBe("");
    expect(screen.getByRole("link", { name: "Recycle bin (4)" })).toHaveClass("bin-pulse");
    // The other row never moves.
    expect(screen.getByRole("button", { name: "Select: Data Analyst" })).toBeInTheDocument();

    await waitFor(() => expect(screen.queryByTestId("leaving-row")).toBeNull());
    expect(screen.queryByRole("button", { name: "Select: Senior AI Engineer" })).not.toBeInTheDocument();
  });

  it("removes the row at once under reduced motion", async () => {
    reduceMotion(true);
    server([page(ROWS, 3)]);
    await dismiss();

    await waitFor(() =>
      expect(screen.queryByText("Senior AI Engineer")).not.toBeInTheDocument(),
    );
    expect(screen.queryByTestId("leaving-row")).toBeNull();
    expect(screen.getByRole("link", { name: "Recycle bin (4)" })).not.toHaveClass("bin-pulse");
  });

  it("animates the rows the chat removed, then drops them", async () => {
    reduceMotion(false);
    server(
      [page(ROWS, 0), page([ROWS[1]], 1)],
      [{ kind: "jobs_changed", terms: ["analyst"], count: 1 }],
    );
    render(<ReviewPage />);
    await screen.findByRole("button", { name: "Select: Senior AI Engineer" });

    fireEvent.click(screen.getByRole("button", { name: "Open Copilot chat" }));
    const box = screen.getByLabelText("Message to the assistant");
    fireEvent.change(box, { target: { value: "drop AI roles" } });
    fireEvent.keyDown(box, { key: "Enter" });

    const ghost = await screen.findByTestId("leaving-row");
    expect(ghost).toHaveTextContent("Senior AI Engineer");
    await waitFor(() => expect(screen.queryByTestId("leaving-row")).toBeNull());
    expect(screen.getByRole("button", { name: "Select: Data Analyst" })).toBeInTheDocument();
  });
});
