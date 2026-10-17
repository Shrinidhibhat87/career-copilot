import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewPage from "@/app/recommendations/page";
import type {
  JobCard,
  ReviewPage as ReviewPageData,
  ReviewRow,
} from "@/lib/api";

function response(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body } as Response;
}

function row(overrides: Partial<ReviewRow> = {}): ReviewRow {
  return {
    id: 1,
    title: "Senior AI Engineer",
    company: "Example GmbH",
    companyUrl: null,
    location: "Berlin",
    source: "greenhouse",
    url: "https://example.test/1",
    firstSeenAt: new Date().toISOString(),
    lastSeenAt: new Date().toISOString(),
    outcome: "pass",
    score: 82,
    scoreStage: "match",
    verdict: "strong",
    why: "Owns an LLM retrieval platform end to end.",
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
    ...overrides,
  };
}

function page(overrides: Partial<ReviewPageData> = {}): ReviewPageData {
  return {
    items: [row()],
    inactiveAfterDays: 21,
    threshold: 30,
    limit: 10,
    kept: 1,
    dropped: 3,
    below: 2,
    unscored: 1,
    total: 4,
    binCount: 0,
    ...overrides,
  };
}

function card(overrides: Partial<JobCard> = {}): JobCard {
  return {
    ...row(),
    description: "You will own the retrieval and evaluation platform.",
    employmentType: "full-time",
    remote: false,
    lastSeenAt: new Date().toISOString(),
    contentHash: "abc",
    filterReasons: [],
    filterWarnings: ["also listed: london"],
    triageScore: 70,
    triageReasons: [],
    matchScore: 82,
    reasons: ["Python platform ownership"],
    gaps: ["German listed as preferred"],
    warnings: [],
    evidence: ["Built and operated the ingestion pipeline"],
    rubricVersion: 1,
    parts: null,
    model: "test-model",
    scoredAt: new Date().toISOString(),
    costEur: 0.04,
    note: null,
    labelledAt: null,
    notionIndex: null,
    ...overrides,
  };
}

describe("review screen", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("counts recommendations, and appends tagged lower matches but never unscored rows", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("includeBelow=true")) {
        return response(
          page({
            items: [
              row(),
              row({
                id: 2,
                title: "Data Analyst",
                score: 20,
                belowThreshold: true,
              }),
              row({
                id: 3,
                title: "Unread Role",
                score: null,
                scoreStage: null,
                verdict: null,
              }),
            ],
          }),
        );
      }
      if (url.includes("/api/review")) return response(page());
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewPage />);

    expect(await screen.findByRole("button", { name: "Select: Senior AI Engineer" })).toBeInTheDocument();
    expect(screen.getByText("1 RESULT")).toBeInTheDocument();
    expect(screen.queryByText(/Showing/)).not.toBeInTheDocument();
    expect(screen.queryByText(/above ·/)).not.toBeInTheDocument();
    expect(screen.queryByText("Lower match")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show lower matches" }));
    expect(await screen.findByText("Data Analyst")).toBeInTheDocument();
    const lower = screen.getByText("Data Analyst").closest("li");
    expect(lower).toHaveTextContent("Lower match");
    expect(screen.queryByText("Unread Role")).not.toBeInTheDocument();
    // The count is what the list shows now, lower matches included.
    expect(screen.getByText("2 RESULTS")).toBeInTheDocument();
  });

  it("shows no machinery text on the list or the card", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/api/jobs/1")
          ? response(card())
          : response(page()),
      ),
    );
    const { container } = render(<ReviewPage />);
    await screen.findByRole("button", { name: "Select: Senior AI Engineer" });
    const forbidden = /Showing|Rules:|Model:|Rubric|€|MODEL NOT CALLED/i;
    expect(container.textContent).not.toMatch(forbidden);

    fireEvent.click(
      screen.getByRole("button", { name: "Details: Senior AI Engineer" }),
    );
    await screen.findByText("Python platform ownership");
    expect(container.textContent).not.toMatch(forbidden);
    expect(
      screen.queryByText("Deterministic rules applied"),
    ).not.toBeInTheDocument();
  });

  it("links the company in a new tab when its url is known, else shows plain text", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        response(
          page({
            items: [
              row({ companyUrl: "https://example.com" }),
              row({
                id: 2,
                title: "Other Role",
                company: "Plain Co",
                companyUrl: null,
              }),
            ],
          }),
        ),
      ),
    );
    render(<ReviewPage />);

    const link = await screen.findByRole("link", { name: "Example GmbH" });
    expect(link).toHaveAttribute("href", "https://example.com");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText(/Plain Co/)).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Plain Co" }),
    ).not.toBeInTheDocument();
  });

  it("tints a location mismatch red and says so, and leaves a matching row alone", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        response(
          page({
            items: [
              row(),
              row({
                id: 2,
                title: "Toronto Role",
                location: "Toronto",
                score: 9,
                belowThreshold: true,
                locationMismatch: true,
              }),
            ],
          }),
        ),
      ),
    );
    render(<ReviewPage />);

    const mismatch = (
      await screen.findByRole("button", { name: "Select: Toronto Role" })
    ).closest("li");
    expect(mismatch).toHaveClass("location-mismatch");
    expect(mismatch).toHaveTextContent("Location mismatch");
    const fine = screen
      .getByRole("button", { name: "Select: Senior AI Engineer" })
      .closest("li");
    expect(fine).not.toHaveClass("location-mismatch");
    expect(fine).not.toHaveTextContent("Location mismatch");
  });

  it("gives refresh a circular-arrow icon, and the page links a lighter style", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => response(page())),
    );
    render(<ReviewPage />);
    await screen.findByRole("button", { name: "Select: Senior AI Engineer" });

    const refresh = screen.getByRole("button", { name: "Look for new jobs" });
    expect(refresh.querySelector("svg.refresh-icon")).not.toBeNull();
    expect(refresh).toHaveClass("refresh-button");
    expect(screen.getByRole("link", { name: "Update CV" })).toHaveClass(
      "soft-link",
    );
    expect(screen.getByRole("link", { name: "Edit preferences" })).toHaveClass(
      "soft-link",
    );
    expect(
      screen.getByRole("heading", { name: "Recommendations" }).parentElement,
    ).toHaveClass("review-title");
  });

  it("links to the recycle bin with its item count", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(page({ binCount: 3 }))));
    render(<ReviewPage />);
    await screen.findByRole("button", { name: "Select: Senior AI Engineer" });

    expect(screen.getByRole("link", { name: "Recycle bin (3)" })).toHaveAttribute(
      "href",
      "/recycle-bin",
    );
  });

  it("drops the stored-as-given footnote and keeps the source name on one line", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => response(page())),
    );
    const { container } = render(<ReviewPage />);
    await screen.findByRole("button", { name: "Select: Senior AI Engineer" });

    expect(container.textContent).not.toMatch(
      /stored as given|re-rank nothing/,
    );
    expect(screen.getByText("greenhouse")).toHaveClass("source-name");
  });

  it("links to the CV and preferences pages and names itself Recommendations", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => response(page())),
    );
    render(<ReviewPage />);
    await screen.findByRole("button", { name: "Select: Senior AI Engineer" });

    expect(
      screen.getByRole("heading", { name: "Recommendations" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Update CV" })).toHaveAttribute(
      "href",
      "/resume",
    );
    expect(
      screen.getByRole("link", { name: "Edit preferences" }),
    ).toHaveAttribute("href", "/preferences");
  });

  it("takes a role title in the list straight to the original posting", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/review")) return response(page());
      throw new Error(`unexpected request: ${String(input)}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewPage />);

    const title = await screen.findByRole("link", {
      name: "Senior AI Engineer",
    });
    expect(title).toHaveAttribute("href", "https://example.test/1");
    expect(title).toHaveAttribute("target", "_blank");
    // The pane title is the posting; the card is still a click away, so nothing is lost.
    expect(
      screen.getByRole("button", { name: "Details: Senior AI Engineer" }),
    ).toBeInTheDocument();
  });

  it("warns that an old posting may already be closed", async () => {
    const stale = new Date(Date.now() - 14 * 86_400_000).toISOString();
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/review")) {
        return response(
          page({
            items: [
              row(),
              row({ id: 2, title: "Stale Role", lastSeenAt: stale }),
            ],
          }),
        );
      }
      throw new Error(`unexpected request: ${String(input)}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewPage />);

    // 14 days unseen against a 21-day horizon: the board has very likely taken it down,
    // and the link would land on the board's own error page.
    const staleRow = (
      await screen.findByRole("button", { name: "Select: Stale Role" })
    ).closest("li");
    expect(staleRow).toHaveTextContent("MAY BE CLOSED");
    const fresh = screen
      .getByRole("button", { name: "Select: Senior AI Engineer" })
      .closest("li");
    // The first row is the one the detail pane shows, and it carries the last-seen age.
    expect(screen.getByText(/last seen today/)).toBeInTheDocument();
    expect(fresh).not.toHaveTextContent("MAY BE CLOSED");
  });

  it("asks for 50 rows by default and keeps the choice from the Show up to selector", async () => {
    const items = Array.from({ length: 10 }, (_, n) =>
      row({ id: n + 1, title: `Role ${n + 1}` }),
    );
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      void input;
      return response(page({ items, kept: 60, total: 60 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewPage />);
    await screen.findByRole("button", { name: "Select: Role 1" });
    expect(
      fetchMock.mock.calls.some(([input]) => String(input).includes("limit=50")),
    ).toBe(true);

    fireEvent.change(screen.getByRole("combobox", { name: "Show up to" }), {
      target: { value: "25" },
    });
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([input]) =>
          String(input).includes("limit=25"),
        ),
      ).toBe(true),
    );

    // Changing the threshold must not throw the chosen limit away.
    fireEvent.change(screen.getByRole("slider"), { target: { value: "50" } });
    await waitFor(() => {
      const last = String(fetchMock.mock.calls.at(-1)?.[0]);
      expect(last).toContain("threshold=50");
      expect(last).toContain("limit=25");
    });
  });

  it("moves the threshold and asks the server for the new cut", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      void input;
      return response(page());
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewPage />);
    await screen.findByRole("button", { name: "Select: Senior AI Engineer" });

    fireEvent.change(screen.getByRole("slider"), {
      target: { value: "70" },
    });
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([input]) =>
          String(input).includes("threshold=70"),
        ),
      ).toBe(true),
    );
  });

  it("saves a row, says it is pinned, and keeps it in the list", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/reviews") && init?.method === "POST") {
          return response(
            {
              reviewId: 1,
              label: "good_fit",
              decision: "saved",
              rerankedNothing: true,
            },
            true,
            201,
          );
        }
        return response(page());
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewPage />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Good fit: Senior AI Engineer",
      }),
    );
    expect(
      await screen.findByText(/stays pinned at the top/),
    ).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Saved" })).toBeInTheDocument();

    const call = fetchMock.mock.calls.find(([input]) =>
      String(input).includes("/api/reviews"),
    );
    expect(JSON.parse(call?.[1]?.body as string)).toEqual({
      jobId: 1,
      label: "good_fit",
      note: "",
    });
  });

  it("offers why after a dismissal and sends the chosen reason", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/reviews") && init?.method === "POST") {
          return response(
            {
              reviewId: 1,
              label: "not_relevant",
              decision: "dismissed",
              rerankedNothing: true,
            },
            true,
            201,
          );
        }
        if (url.includes("/decision") && init?.method === "POST") {
          return response({ decisionId: 2, decision: "dismissed" }, true, 201);
        }
        return response(page());
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewPage />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Not relevant: Senior AI Engineer",
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Seniority" }));

    await screen.findByText(/take that into account/);
    const call = fetchMock.mock.calls.find(([input]) =>
      String(input).includes("/decision"),
    );
    expect(JSON.parse(call?.[1]?.body as string)).toEqual({
      decision: "dismissed",
      reason: "seniority",
    });
    expect(
      screen.queryByRole("group", { name: "Why was it dismissed?" }),
    ).not.toBeInTheDocument();
  });

  it("narrows the list to a company typed in the search box", async () => {
    const fetchMock = vi.fn(async () =>
      response(
        page({
          items: [
            row({ id: 1, title: "Backend Engineer", company: "Helsing" }),
            row({ id: 2, title: "Data Engineer", company: "Acme GmbH" }),
          ],
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewPage />);

    await screen.findAllByText("Backend Engineer");
    fireEvent.change(screen.getByLabelText("Search companies"), {
      target: { value: "hels" },
    });

    expect(screen.getAllByText("Backend Engineer").length).toBeGreaterThan(0);
    expect(screen.queryByText("Data Engineer")).not.toBeInTheDocument();
  });

  it("asks why after a dismissal made inside the full-details card", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/reviews") && init?.method === "POST") {
          return response(
            { reviewId: 1, label: "not_relevant", decision: "dismissed", rerankedNothing: true },
            true,
            201,
          );
        }
        if (url.includes("/api/jobs/1")) return response(card());
        return response(page());
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "Details: Senior AI Engineer" }),
    );
    await screen.findByText("Python platform ownership");
    const overlay = document.querySelector(".card-overlay") as HTMLElement;
    fireEvent.click(
      within(overlay).getByRole("button", { name: /Not relevant/ }),
    );

    expect(
      await screen.findByRole("group", { name: "Why was it dismissed?" }),
    ).toBeInTheDocument();
  });

  it("toggles Interested: a second press goes back to maybe", async () => {
    const labels: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/api/reviews") && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as { label: string };
          labels.push(body.label);
          return response({ reviewId: 1, label: body.label, decision: "saved", rerankedNothing: true }, true, 201);
        }
        return response(page());
      }),
    );
    render(<ReviewPage />);
    const button = await screen.findByRole("button", {
      name: "Good fit: Senior AI Engineer",
    });
    fireEvent.click(button);
    await waitFor(() => expect(button).toHaveAttribute("aria-pressed", "true"));
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(button).toHaveAttribute("aria-pressed", "false"));
    expect(labels).toEqual(["good_fit", "maybe"]);
    expect(await screen.findByText(/No longer interested/)).toBeInTheDocument();
  });

  it("opens the card with its reasons, its evidence behind a disclosure, and its gaps", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/jobs/1")) return response(card());
      return response(page());
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewPage />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Details: Senior AI Engineer",
      }),
    );
    expect(await screen.findByText("Python platform ownership")).toBeVisible();
    expect(screen.getByText("From your CV")).toBeInTheDocument();
    expect(
      screen.getByText(/Built and operated the ingestion pipeline/),
    ).not.toBeVisible();
    expect(screen.getByText("German listed as preferred")).toBeInTheDocument();
  });

  it("keeps the scroll position when moving to the next posting", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/jobs/1")) return response(card());
      if (url.includes("/api/jobs/2")) return response(card({ id: 2, title: "Data Engineer" }));
      return response(page({ items: [row(), row({ id: 2, title: "Data Engineer" })] }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    vi.spyOn(window, "scrollY", "get").mockReturnValue(420);
    render(<ReviewPage />);

    fireEvent.click(
      await screen.findByRole("button", { name: "Details: Senior AI Engineer" }),
    );
    await screen.findByText("Python platform ownership");
    expect(scrollTo).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /Next posting/ }));
    await waitFor(() => expect(scrollTo).toHaveBeenCalledWith(0, 420));
    vi.restoreAllMocks();
  });

  it("replaces the claims with the reason there are none when rules decided it", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/jobs/1")) {
        return response(
          card({
            outcome: "reject",
            reasons: [],
            triageReasons: [],
            matchScore: null,
            triageScore: null,
            filterReasons: ["location outside Germany: Paris"],
          }),
        );
      }
      return response(page());
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewPage />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Details: Senior AI Engineer",
      }),
    );
    expect(
      await screen.findByText("No match claims — decided by rules."),
    ).toBeInTheDocument();
  });

  async function openChat() {
    await screen.findByRole("button", { name: "Select: Senior AI Engineer" });
    fireEvent.click(screen.getByRole("button", { name: "Open Copilot chat" }));
    return screen.getByLabelText("Message to the assistant");
  }

  function assistantFetch(reply: () => Promise<Response> | Response) {
    return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
      String(input).includes("/api/assistant") && init?.method === "POST"
        ? reply()
        : response(page()),
    );
  }

  it("keeps the chat closed until the launcher is clicked", async () => {
    vi.stubGlobal(
      "fetch",
      assistantFetch(() => response({})),
    );
    render(<ReviewPage />);
    await screen.findByRole("button", { name: "Select: Senior AI Engineer" });
    expect(
      screen.queryByLabelText("Message to the assistant"),
    ).not.toBeInTheDocument();
    await openChat();
    fireEvent.click(screen.getByRole("button", { name: "Close Copilot chat" }));
    expect(
      screen.queryByLabelText("Message to the assistant"),
    ).not.toBeInTheDocument();
  });

  it("sends on Enter, shows ..., then the reply, and applies set_view", async () => {
    const calls: string[] = [];
    let release: (value: Response) => void = () => undefined;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        if (
          String(input).includes("/api/assistant") &&
          init?.method === "POST"
        ) {
          return new Promise<Response>((resolve) => {
            release = resolve;
          });
        }
        calls.push(String(input));
        return response(page());
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewPage />);
    const box = await openChat();

    fireEvent.change(box, { target: { value: "5 more please" } });
    fireEvent.keyDown(box, { key: "Enter" });

    expect(await screen.findByText("5 more please")).toBeInTheDocument();
    expect(screen.getByLabelText("Copilot is typing")).toBeInTheDocument();

    release(
      response({
        text: "Showing 15, with lower matches.",
        actions: [{ kind: "set_view", limit: 15, includeBelow: true }],
        busy: false,
      }),
    );
    expect(
      await screen.findByText("Showing 15, with lower matches."),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Copilot is typing"),
    ).not.toBeInTheDocument();
    await waitFor(() =>
      expect(
        calls.some(
          (url) =>
            url.includes("limit=15") && url.includes("includeBelow=true"),
        ),
      ).toBe(true),
    );
  });

  it("does not send on Shift+Enter or on an empty box", async () => {
    const fetchMock = assistantFetch(() =>
      response({ text: "hi", actions: [] }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewPage />);
    const box = await openChat();

    fireEvent.keyDown(box, { key: "Enter" });
    fireEvent.change(box, { target: { value: "no fintech" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });

    const posted = fetchMock.mock.calls.filter(([input]) =>
      String(input).includes("/api/assistant"),
    );
    expect(posted).toHaveLength(0);
  });

  it("keeps Send disabled while the box is empty and lights it once typed", async () => {
    vi.stubGlobal(
      "fetch",
      assistantFetch(() => response({})),
    );
    render(<ReviewPage />);
    const box = await openChat();

    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toBeDisabled();
    fireEvent.change(box, { target: { value: "no fintech" } });
    expect(send).toBeEnabled();
  });

  it("warns that replies may be slow while a search is running", async () => {
    vi.stubGlobal(
      "fetch",
      assistantFetch(() =>
        response({ text: "Showing 15.", actions: [], busy: true }),
      ),
    );
    render(<ReviewPage />);
    const box = await openChat();
    fireEvent.change(box, { target: { value: "5 more" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("Showing 15.")).toBeInTheDocument();
    expect(
      screen.getByText(/A search is running, replies may be slow/),
    ).toBeInTheDocument();
  });

  it("says the assistant needs a local model on a 409", async () => {
    vi.stubGlobal(
      "fetch",
      assistantFetch(() =>
        response(
          {
            detail: { errors: { model: "The assistant needs a local model" } },
          },
          false,
          409,
        ),
      ),
    );
    render(<ReviewPage />);
    const box = await openChat();
    fireEvent.change(box, { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The assistant needs a local model",
    );
  });

  it("names the review service when it cannot be reached", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("network down");
      }),
    );
    render(<ReviewPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /review service/,
    );
  });
});

describe("review lifecycle", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("badges new postings and tells the server they were shown", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) =>
      String(input).includes("/api/jobs/viewed")
        ? response({ marked: 1 })
        : response(page({ items: [row({ id: 7, isNew: true })] })),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewPage />);

    expect((await screen.findAllByText("NEW")).length).toBeGreaterThan(0);
    await waitFor(() => {
      const calls = fetchMock.mock.calls as unknown as [
        RequestInfo | URL,
        RequestInit,
      ][];
      const call = calls.find(([input]) =>
        String(input).includes("/api/jobs/viewed"),
      );
      expect(JSON.parse(call?.[1]?.body as string)).toEqual({ ids: [7] });
    });
  });

  it("does not report postings that are already viewed", async () => {
    const fetchMock = vi.fn(async () => response(page()));
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewPage />);
    await screen.findByRole("button", { name: "Select: Senior AI Engineer" });
    const urls = (fetchMock.mock.calls as unknown as [RequestInfo | URL][]).map(
      ([input]) => String(input),
    );
    expect(urls.some((url) => url.includes("/api/jobs/viewed"))).toBe(false);
  });

  it("shows a saved posting pinned", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        response(page({ items: [row({ isSaved: true, decision: "saved" })] })),
      ),
    );
    render(<ReviewPage />);
    expect(
      await screen.findByRole("img", { name: "Saved" }),
    ).toBeInTheDocument();
  });

  it("removes a dismissed posting from the list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
        String(input).includes("/api/reviews") && init?.method === "POST"
          ? response(
              {
                reviewId: 1,
                label: "not_relevant",
                decision: "dismissed",
                rerankedNothing: true,
              },
              true,
              201,
            )
          : response(page()),
      ),
    );
    render(<ReviewPage />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Not relevant: Senior AI Engineer",
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Select: Senior AI Engineer" })).not.toBeInTheDocument(),
    );
    expect(screen.getByText(/will not be shown again/)).toBeInTheDocument();
  });
});

describe("applied and notion", () => {
  beforeEach(() => vi.unstubAllGlobals());

  /** A fake API: the list, the decision route and the Notion route, each recorded. */
  function server(notion: () => Response = () => response({ status: "added", index: 1 })) {
    const calls: { url: string; body: unknown }[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (url.includes("/decision")) {
        return response({ decisionId: 1, decision: "applied" }, true, 201);
      }
      if (url.includes("/notion")) return notion();
      if (url.includes("/api/review")) return response(page());
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    return calls;
  }

  const notionCalls = (calls: { url: string }[]) =>
    calls.filter((call) => call.url.includes("/notion"));

  async function markApplied() {
    render(<ReviewPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Applied: Senior AI Engineer" }),
    );
    return screen.findByRole("dialog", {
      name: "Add this to your Notion Applications database?",
    });
  }

  it("saves Applied, badges the row, and asks about Notion", async () => {
    const calls = server();
    await markApplied();
    const decision = calls.find((call) => call.url.includes("/decision"));
    expect(decision?.url).toContain("/api/jobs/1/decision");
    expect(decision?.body).toEqual({ decision: "applied" });
    const row = screen
      .getByRole("button", { name: "Select: Senior AI Engineer" })
      .closest("li");
    expect(row).toHaveTextContent("APPLIED");
    expect(notionCalls(calls)).toHaveLength(0);
  });

  it("asks the Notion question next to the Applied button, after saying it is applied", async () => {
    server();
    const dialog = await markApplied();
    const button = screen.getByRole("button", { name: "Applied: Senior AI Engineer" });
    expect(button.closest(".detail-applied")).toContainElement(dialog);
    expect(within(dialog).getByText(/Marked applied/)).toBeInTheDocument();
  });

  it("sends no Notion call on No", async () => {
    const calls = server();
    const dialog = await markApplied();
    fireEvent.click(within(dialog).getByRole("button", { name: "No" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(notionCalls(calls)).toHaveLength(0);
  });

  it("sends exactly one Notion call on Yes and says which row it became", async () => {
    const calls = server();
    const dialog = await markApplied();
    fireEvent.click(within(dialog).getByRole("button", { name: "Yes" }));
    expect(await within(dialog).findByText(/row 1/)).toBeInTheDocument();
    expect(notionCalls(calls)).toHaveLength(1);
    expect(notionCalls(calls)[0].url).toContain("/api/jobs/1/notion");
  });

  it("shows the reason and Add to Notion when Notion fails, and retries on click", async () => {
    const reason = "The Notion key cannot write. Tick Insert content.";
    const calls = server(() =>
      response({ detail: { errors: { notion: reason } } }, false, 502),
    );
    const dialog = await markApplied();
    fireEvent.click(within(dialog).getByRole("button", { name: "Yes" }));
    expect(await within(dialog).findByText(reason)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Add to Notion" }));
    await waitFor(() => expect(notionCalls(calls)).toHaveLength(2));
    const row = screen
      .getByRole("button", { name: "Select: Senior AI Engineer" })
      .closest("li");
    expect(row).toHaveTextContent("APPLIED");
  });

  it("hides applied jobs until the Applied pill narrows the list to them", async () => {
    appliedServer({ score: 82, belowThreshold: false });
    render(<ReviewPage />);
    await screen.findByText("No matches for these filters");
    expect(
      screen.queryByRole("button", { name: "Select: Senior AI Engineer" }),
    ).not.toBeInTheDocument();
    const filters = screen.getByRole("group", { name: "Filter" });
    const pill = within(filters).getByRole("button", { name: "Applied" });
    expect(pill).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(pill);
    expect(pill).toHaveAttribute("aria-pressed", "true");
    expect(
      await screen.findByRole("button", { name: "Select: Senior AI Engineer" }),
    ).toBeInTheDocument();
  });

  it("the Applied pill lists only applied jobs, and is empty when none is applied", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        response(page({ items: [row({ id: 1, title: "Open Role" })] })),
      ),
    );
    render(<ReviewPage />);
    expect(
      await screen.findByRole("button", { name: "Select: Open Role" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Applied" }));
    expect(await screen.findByText("No matches for these filters")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Select: Open Role" }),
    ).not.toBeInTheDocument();
  });

  /** One applied row; the undo route answers with `restored`. */
  function appliedServer(overrides: Partial<ReviewRow> = {}, restored: string | null = null) {
    const calls: { url: string; body: unknown }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
        if (url.includes("/undo-applied")) return response({ decision: restored });
        if (url.includes("/decision")) throw new Error("undo must not post a decision");
        return response(
          page({
            items: [
              row({
                score: 20,
                belowThreshold: true,
                decision: "applied",
                isApplied: true,
                ...overrides,
              }),
            ],
          }),
        );
      }),
    );
    return calls;
  }

  /** Applied postings are hidden until the Applied pill is on. */
  async function showAppliedRows() {
    fireEvent.click(await screen.findByRole("button", { name: "Applied" }));
  }

  it("unchecks Applied only after a confirmation, and Keep applied sends nothing", async () => {
    const calls = appliedServer();
    render(<ReviewPage />);
    await showAppliedRows();
    const applied = await screen.findByRole("button", {
      name: "Applied: Senior AI Engineer",
    });
    expect(applied).toHaveAttribute("aria-pressed", "true");
    expect(applied).toBeEnabled();
    const undos = () => calls.filter((call) => call.url.includes("/undo-applied"));

    fireEvent.click(applied);
    let confirm = await screen.findByRole("alertdialog", { name: "Undo applied?" });
    expect(applied.closest(".detail-applied")).toContainElement(confirm);
    fireEvent.click(within(confirm).getByRole("button", { name: "Keep applied" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(undos()).toHaveLength(0);

    fireEvent.click(applied);
    confirm = await screen.findByRole("alertdialog", { name: "Undo applied?" });
    fireEvent.click(within(confirm).getByRole("button", { name: "Undo applied" }));
    await waitFor(() => expect(applied).toHaveAttribute("aria-pressed", "false"));
    expect(undos()).toHaveLength(1);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("undo puts the row back exactly as it was: not saved, not pinned", async () => {
    appliedServer();
    render(<ReviewPage />);
    await showAppliedRows();
    const applied = await screen.findByRole("button", {
      name: "Applied: Senior AI Engineer",
    });
    fireEvent.click(applied);
    const confirm = await screen.findByRole("alertdialog", { name: "Undo applied?" });
    expect(confirm).not.toHaveTextContent(/Notion/);
    fireEvent.click(within(confirm).getByRole("button", { name: "Undo applied" }));
    await waitFor(() => expect(applied).toHaveAttribute("aria-pressed", "false"));
    const listed = screen.getByRole("button", { name: "Select: Senior AI Engineer" }).closest("li");
    expect(listed).not.toHaveTextContent("APPLIED");
    expect(listed).not.toHaveClass("saved-row");
    expect(screen.queryByRole("img", { name: "Saved" })).not.toBeInTheDocument();
    expect(screen.getByText(/Applied undone/)).not.toHaveTextContent(/Notion/);
  });

  it("undo restores a saved job as saved", async () => {
    appliedServer({}, "saved");
    render(<ReviewPage />);
    await showAppliedRows();
    const applied = await screen.findByRole("button", {
      name: "Applied: Senior AI Engineer",
    });
    fireEvent.click(applied);
    const confirm = await screen.findByRole("alertdialog", { name: "Undo applied?" });
    fireEvent.click(within(confirm).getByRole("button", { name: "Undo applied" }));
    expect(await screen.findByRole("img", { name: "Saved" })).toBeInTheDocument();
  });

  it("mentions the Notion row only when the job is in Notion", async () => {
    appliedServer({ notionIndex: 2 });
    render(<ReviewPage />);
    await showAppliedRows();
    fireEvent.click(
      await screen.findByRole("button", { name: "Applied: Senior AI Engineer" }),
    );
    const confirm = await screen.findByRole("alertdialog", { name: "Undo applied?" });
    expect(confirm).toHaveTextContent(/row 2/);
  });

  it("keeps an unscored applied posting in the list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        response(
          page({
            items: [
              row({
                score: null,
                scoreStage: null,
                decision: "applied",
                isApplied: true,
              }),
            ],
          }),
        ),
      ),
    );
    render(<ReviewPage />);
    await showAppliedRows();
    expect(await screen.findByRole("button", { name: "Select: Senior AI Engineer" })).toBeInTheDocument();
  });
});

describe("split view", () => {
  beforeEach(() => vi.unstubAllGlobals());

  const two = () =>
    page({
      items: [row(), row({ id: 2, title: "Data Engineer", why: "Owns pipelines." })],
    });

  it("shows the selected posting in the pane and follows a click", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(two())));
    render(<ReviewPage />);
    const pane = await screen.findByRole("complementary", { name: "Selected posting" });
    expect(within(pane).getByText("Owns an LLM retrieval platform end to end.")).toBeInTheDocument();
    expect(within(pane).getByRole("link", { name: "Senior AI Engineer" })).toHaveAttribute(
      "href",
      "https://example.test/1",
    );

    fireEvent.click(screen.getByRole("button", { name: "Select: Data Engineer" }));
    // The pane remounts per posting, so it is looked up again.
    const next = screen.getByRole("complementary", { name: "Selected posting" });
    expect(within(next).getByText("Owns pipelines.")).toBeInTheDocument();
  });

  it("opens the full card over the list and closes it again", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/api/jobs/1") ? response(card()) : response(two()),
      ),
    );
    render(<ReviewPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Details: Senior AI Engineer" }));
    expect(await screen.findByRole("dialog", { name: "Full details" })).toBeInTheDocument();
    expect(await screen.findByText("Python platform ownership")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "← Back to list" })).not.toBeInTheDocument();

    // Pressing inside the card keeps it open.
    const dialog = screen.getByRole("dialog", { name: "Full details" });
    const inside = screen.getByText("Python platform ownership");
    fireEvent.mouseDown(inside);
    fireEvent.click(inside);
    expect(dialog).toBeInTheDocument();

    // Pressing the dimmed area outside the card goes back to the list.
    fireEvent.mouseDown(dialog);
    fireEvent.click(dialog);
    expect(screen.queryByRole("dialog", { name: "Full details" })).not.toBeInTheDocument();
  });

  it("a drag that starts inside the card and ends outside leaves it open", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/api/jobs/1") ? response(card()) : response(two()),
      ),
    );
    render(<ReviewPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Details: Senior AI Engineer" }));
    const dialog = await screen.findByRole("dialog", { name: "Full details" });
    fireEvent.mouseDown(await screen.findByText("Python platform ownership"));
    fireEvent.click(dialog);
    expect(dialog).toBeInTheDocument();
  });

  it("ctrl-click on a row goes to the posting without selecting it", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    vi.stubGlobal("fetch", vi.fn(async () => response(two())));
    render(<ReviewPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Select: Data Engineer" }), {
      ctrlKey: true,
    });
    expect(open).toHaveBeenCalledWith("https://example.test/1", "_blank", "noopener,noreferrer");
  });

  it("offers Reset filters only once a filter moved, and resets it", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(two())));
    render(<ReviewPage />);
    await screen.findByRole("button", { name: "Select: Data Engineer" });
    expect(screen.queryByRole("button", { name: "Reset filters" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("slider"), { target: { value: "70" } });
    fireEvent.click(await screen.findByRole("button", { name: "Reset filters" }));
    await waitFor(() => expect(screen.getByRole("slider")).toHaveValue("30"));
  });
});

describe("location and interested filters", () => {
  beforeEach(() => vi.unstubAllGlobals());

  const mixed = () =>
    page({
      items: [
        row({ id: 1, title: "Berlin Role", location: "Berlin, Berlin, Germany" }),
        row({ id: 2, title: "Munich Role", location: "München" }),
        row({ id: 3, title: "Remote Role", location: "Remote US, West Coast", isSaved: true, decision: "saved", label: "good_fit" }),
      ],
    });

  it("narrows the list to the picked cities, with counts, and clears", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(mixed())));
    render(<ReviewPage />);
    await screen.findByRole("button", { name: "Select: Berlin Role" });

    fireEvent.click(screen.getByRole("button", { name: "Location: ALL" }));
    const menu = screen.getByRole("group", { name: "Choose locations" });
    expect(within(menu).getByLabelText(/Munich/)).toBeInTheDocument();
    fireEvent.click(within(menu).getByLabelText(/Munich/));

    expect(screen.getByRole("button", { name: "Location: MUNICH" })).toBeInTheDocument();
    expect(screen.getByText("1 RESULT")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Select: Berlin Role" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Select: Munich Role" })).toBeInTheDocument();

    fireEvent.click(within(menu).getByRole("button", { name: "Clear" }));
    expect(screen.getByText("3 RESULTS")).toBeInTheDocument();
  });

  it("shows only interested postings with the Interested pill", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(mixed())));
    render(<ReviewPage />);
    await screen.findByRole("button", { name: "Select: Berlin Role" });

    const filters = screen.getByRole("group", { name: "Filter" });
    fireEvent.click(within(filters).getByRole("button", { name: "Interested" }));
    expect(screen.getByText("1 RESULT")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Select: Remote Role" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Select: Berlin Role" })).not.toBeInTheDocument();
  });
});
