import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ReviewCard } from "@/components/review-card";
import { cardNext, cardPrev } from "@/lib/pager";
import type { JobCard, ScoreParts } from "@/lib/api";

function response(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body } as Response;
}

function card(overrides: Partial<JobCard> = {}): JobCard {
  return {
    id: 1,
    title: "Senior AI Engineer",
    company: "Example GmbH",
    companyUrl: "https://example.com",
    location: "Berlin",
    source: "greenhouse",
    url: "https://example.test/1",
    firstSeenAt: new Date().toISOString(),
    lastSeenAt: new Date().toISOString(),
    outcome: "pass",
    score: 82,
    scoreStage: "match",
    verdict: "strong",
    why: "Owns an LLM retrieval platform.",
    label: null,
    labelStale: false,
    belowThreshold: false,
    locationMismatch: false,
    decision: null,
    isSaved: false,
    isApplied: false,
    isNew: false,
    viewedAt: null,
    scoredWithCv: 2,
    description: "You will own the retrieval platform.",
    employmentType: "full-time",
    remote: false,
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
    rubricVersion: 2,
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

const PARTS: ScoreParts = {
  skills: { points: 30, reason: "Python and LangGraph match the posting." },
  seniority: { points: 20, reason: "Senior scope, as advertised." },
  domain: { points: 15, reason: "Adjacent industry." },
  preferences: { points: 10, reason: "Berlin and hybrid." },
};

function props(overrides: Partial<Parameters<typeof ReviewCard>[0]> = {}) {
  return {
    inactiveAfterDays: 21,
    jobId: 1,
    index: 0,
    total: 3,
    onGo: vi.fn(),
    onLabelled: vi.fn(),
    onDecided: vi.fn(),
    ...overrides,
  };
}

describe("card pager arithmetic", () => {
  it("wraps in both directions", () => {
    expect(cardNext(2, 3)).toBe(0);
    expect(cardNext(0, 3)).toBe(1);
    expect(cardPrev(0, 3)).toBe(2);
    expect(cardPrev(2, 3)).toBe(1);
  });
});

describe("review card", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("shows n / N and wraps from the last card to the first and back", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(card())));
    const last = props({ index: 2, total: 3 });
    const { unmount } = render(<ReviewCard {...last} />);
    expect(await screen.findByText("3 / 3")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next posting" }));
    expect(last.onGo).toHaveBeenCalledWith(0);
    unmount();

    const first = props({ index: 0, total: 3 });
    render(<ReviewCard {...first} />);
    await screen.findByText("1 / 3");
    fireEvent.click(screen.getByRole("button", { name: "Previous posting" }));
    expect(first.onGo).toHaveBeenCalledWith(2);
  });

  it("disables both arrows when there is only one card", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(card())));
    render(<ReviewCard {...props({ index: 0, total: 1 })} />);
    await screen.findByText("1 / 1");
    expect(screen.getByRole("button", { name: "Next posting" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Previous posting" }),
    ).toBeDisabled();
  });

  it("shows the responsibilities and requirements instead of the posting's opening", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
        String(input).includes("/outline") && init?.method === "POST"
          ? response({
              status: "ok",
              responsibilities: ["Design backend services."],
              requirements: [],
            })
          : response(card({ description: "KINEXON stands as a global pioneer." })),
      ),
    );
    render(<ReviewCard {...props()} />);

    expect(await screen.findByText("Design backend services.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Responsibilities" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Requirements" })).toBeInTheDocument();
    expect(screen.getByText("Not stated in the posting")).toBeInTheDocument();
    expect(screen.queryByText(/global pioneer/)).not.toBeInTheDocument();
  });

  it("says the summary is not working, and does not fall back to the opening text", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
        String(input).includes("/outline") && init?.method === "POST"
          ? response({ status: "unavailable" })
          : response(card({ description: "KINEXON stands as a global pioneer." })),
      ),
    );
    render(<ReviewCard {...props()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /summary is not working.*original posting/,
    );
    expect(screen.queryByText(/global pioneer/)).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Open original posting/ }),
    ).toHaveAttribute("href", "https://example.test/1");
  });

  it("says the same when the request itself fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
        String(input).includes("/outline") && init?.method === "POST"
          ? response({}, false, 500)
          : response(card()),
      ),
    );
    render(<ReviewCard {...props()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /summary is not working/,
    );
  });

  it("hides the evidence until the disclosure is opened", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(card())));
    render(<ReviewCard {...props()} />);

    const quote = await screen.findByText(
      /Built and operated the ingestion pipeline/,
    );
    expect(quote).not.toBeVisible();
    fireEvent.click(screen.getByText("From your CV"));
    expect(quote).toBeVisible();
  });

  it("turns Evidence is wrong into Noted and posts exactly one correction", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).includes("/api/corrections") && init?.method === "POST") {
          return response({ correctionId: 1 }, true, 201);
        }
        return response(card());
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ReviewCard {...props()} />);

    fireEvent.click(await screen.findByText("From your CV"));
    fireEvent.click(screen.getByRole("button", { name: "Evidence is wrong" }));
    const noted = await screen.findByRole("button", { name: "Noted" });
    expect(noted).toBeDisabled();
    fireEvent.click(noted);

    const posts = fetchMock.mock.calls.filter(([input]) =>
      String(input).includes("/api/corrections"),
    );
    expect(posts).toHaveLength(1);
    expect(JSON.parse(posts[0][1]?.body as string)).toEqual({
      jobId: 1,
      kind: "evidence",
      target: "Python platform ownership",
    });
    expect(screen.queryByText("Mark rule incorrect")).not.toBeInTheDocument();
  });

  it("highlights Maybe, says it was marked, and keeps the job in the list", async () => {
    const onLabelled = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
        String(input).includes("/api/reviews") && init?.method === "POST"
          ? response(
              {
                reviewId: 1,
                label: "maybe",
                decision: "maybe",
                rerankedNothing: true,
              },
              true,
              201,
            )
          : response(card()),
      ),
    );
    render(<ReviewCard {...props({ onLabelled })} />);

    const maybe = await screen.findByRole("button", { name: "Maybe" });
    fireEvent.click(maybe);
    await waitFor(() => expect(maybe).toHaveAttribute("aria-pressed", "true"));
    expect(screen.getByText("Marked maybe")).toBeVisible();
    expect(onLabelled).toHaveBeenCalledWith(1, "maybe");
  });

  it("names the company, linked when its url is known", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(card())));
    render(<ReviewCard {...props()} />);

    const link = await screen.findByRole("link", { name: "Example GmbH" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });
});

describe("review card: applied", () => {
  beforeEach(() => vi.unstubAllGlobals());

  function server(job: JobCard, notion?: () => Response) {
    const calls: { url: string; body: unknown }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const body = init?.body ? JSON.parse(String(init.body)) : null;
        calls.push({ url, body });
        if (url.includes("/undo-applied")) return response({ decision: null });
        if (url.includes("/decision")) {
          return response({ decisionId: 1, decision: body.decision }, true, 201);
        }
        if (url.includes("/notion") && notion) return notion();
        return response(job);
      }),
    );
    return calls;
  }

  it("marks Applied, asks about Notion beside it, and Undo applied restores", async () => {
    const calls = server(card());
    const onDecided = vi.fn();
    render(<ReviewCard {...props({ onDecided })} />);
    fireEvent.click(await screen.findByRole("button", { name: "Applied" }));
    await waitFor(() => expect(onDecided).toHaveBeenCalledWith(1, "applied"));
    expect(screen.getByText("APPLIED")).toBeInTheDocument();
    const ask = await screen.findByRole("dialog", {
      name: "Add this to your Notion Applications database?",
    });
    fireEvent.click(within(ask).getByRole("button", { name: "No" }));

    fireEvent.click(screen.getByRole("button", { name: "Undo applied" }));
    const confirm = await screen.findByRole("alertdialog", { name: "Undo applied?" });
    expect(onDecided).toHaveBeenCalledTimes(1);
    fireEvent.click(within(confirm).getByRole("button", { name: "Undo applied" }));
    await waitFor(() => expect(onDecided).toHaveBeenLastCalledWith(1, null));
    expect(screen.queryByText("APPLIED")).not.toBeInTheDocument();
    expect(calls.filter((call) => call.url.includes("/decision")).map((c) => c.body)).toEqual([
      { decision: "applied" },
    ]);
    expect(calls.filter((call) => call.url.includes("/undo-applied"))).toHaveLength(1);
    expect(calls.some((call) => call.url.includes("/notion"))).toBe(false);
  });

  it("offers Add to Notion for an applied job not yet in Notion, and adds it once", async () => {
    const calls = server(
      card({ decision: "applied", isApplied: true, notionIndex: null }),
      () => response({ status: "added", index: 4 }),
    );
    render(<ReviewCard {...props()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Add to Notion" }));
    expect(await screen.findByText(/row 4/)).toBeInTheDocument();
    expect(calls.filter((call) => call.url.includes("/notion"))).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Add to Notion" })).not.toBeInTheDocument();
  });

  it("shows the Notion row instead of the button once it is there", async () => {
    server(card({ decision: "applied", isApplied: true, notionIndex: 3 }));
    render(<ReviewCard {...props()} />);
    expect(await screen.findByText(/row 3/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add to Notion" })).not.toBeInTheDocument();
  });

  it("keeps Add to Notion and shows the reason when Notion fails", async () => {
    server(card({ decision: "applied", isApplied: true }), () =>
      response({ detail: { errors: { notion: "Add NOTION_API_KEY to .env" } } }, false, 409),
    );
    render(<ReviewCard {...props()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Add to Notion" }));
    expect(await screen.findByText("Add NOTION_API_KEY to .env")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add to Notion" })).toBeInTheDocument();
  });
});

describe("score breakdown", () => {
  beforeEach(() => vi.unstubAllGlobals());

  const withParts = () =>
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => response(card({ score: 75, matchScore: 75, parts: PARTS }))),
    );

  it("shows the total in the donut and each part as points / max", async () => {
    withParts();
    render(<ReviewCard {...props()} />);
    const donut = await screen.findByRole("img", { name: /75 out of 100/ });
    expect(donut).toHaveTextContent("75");
    expect(screen.getByRole("button", { name: /Skills.*30 \/ 40/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Seniority and role.*20 \/ 25/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Domain.*15 \/ 20/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Preferences and location.*10 \/ 15/ })).toBeInTheDocument();
  });

  it("opens a part's sentence on click and closes it on a second click", async () => {
    withParts();
    render(<ReviewCard {...props()} />);
    const skills = await screen.findByRole("button", { name: /Skills/ });
    expect(screen.queryByText(/Python and LangGraph match/)).toBeNull();
    fireEvent.click(skills);
    expect(screen.getByText(/Python and LangGraph match/)).toBeInTheDocument();
    expect(skills).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(skills);
    expect(screen.queryByText(/Python and LangGraph match/)).toBeNull();
  });

  it("opens the same sentence from the arc, and switches parts", async () => {
    withParts();
    const { container } = render(<ReviewCard {...props()} />);
    await screen.findByRole("img", { name: /75 out of 100/ });
    fireEvent.click(container.querySelector('[data-part="domain"]')!);
    expect(screen.getByText(/Adjacent industry/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Preferences/ }));
    expect(screen.queryByText(/Adjacent industry/)).toBeNull();
    expect(screen.getByText(/Berlin and hybrid/)).toBeInTheDocument();
  });

  it("shows the old total and a note, and no donut, for a score without parts", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => response(card({ score: 70, matchScore: 70, rubricVersion: 1, parts: null }))),
    );
    render(<ReviewCard {...props()} />);
    expect(await screen.findByText("Scored before breakdowns existed")).toBeInTheDocument();
    expect(screen.getByText("70")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /out of 100/ })).toBeNull();
  });
});

