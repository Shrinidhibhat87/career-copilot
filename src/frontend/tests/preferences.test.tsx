import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import PreferencesPage from "@/app/preferences/page";
import { NO_MODEL_KEY, PREFERENCES_UNREACHABLE, type Preferences } from "@/lib/api";

function term(value: string, fromResume = false) {
  return { term: value, label: value.toUpperCase(), fromResume };
}

const preferences: Preferences = {
  version: 0,
  updatedAt: null,
  roleIntent: "",
  roleFamilies: [term("ai/ml engineering", true)],
  titleTerms: [term("data engineer")],
  coreSkills: [term("python"), term("kafka", true)],
  excludedStack: [term("java")],
  excludedRoleTerms: [term("frontend")],
  seniority: ["mid", "senior"],
  allowedCities: [term("berlin")],
  preferredCities: [term("berlin")],
  flaggedCities: [term("munich")],
  allowRemote: true,
  requirePermanent: true,
  languages: [
    { name: "English", level: "C2", fromResume: false },
    { name: "German", level: "B1", fromResume: true },
  ],
  maxPerCompany: 2,
  batchSize: 6,
  notes: "Munich only with a Berlin office.",
  seniorityBands: ["junior", "mid", "senior", "staff", "lead"],
  cefrLevels: ["A1", "A2", "B1", "B2", "C1", "C2", "native"],
};

const loaded = { ok: true, status: 200, json: async () => preferences };

// `CompanySummary` (mounted inside `PreferencesForm` once preferences are loaded) reads
// the company list's summary on mount — every test renders through this, so every
// test's fetch-call count is one higher than just "preferences load + its own action".
const companyListConfig = {
  ok: true,
  status: 200,
  json: async () => ({
    listSource: null,
    counts: { resolved: 0, unresolved: 0, blocked: 0, inactive: 0 },
    openRoles: 0,
    foundWithoutRoles: 0,
    companies: [],
    failures: [],
  }),
};

function saved(overrides: Partial<Preferences> = {}, report = {}) {
  return {
    ok: true,
    status: 200,
    json: async () => ({
      preferences: { ...preferences, version: 1, ...overrides },
      refiltered: { examined: 12, changed: 3, transitions: { "reject→pass": 3 }, nowPassing: 3 },
      reopenedForScoring: 3,
      ...report,
    }),
  };
}

// The intent log and sample roles each read their own route on mount, in an order that is
// theirs to choose. They are answered by URL; every other request takes the next queued
// response, in the order the test listed them.
const MOUNT_READS: Record<string, unknown> = {
  "/api/intent": { entries: [], activeWords: 0, condenseAt: 1000 },
  "/api/sample-roles": { roles: [] },
};

function isMountRead(call: unknown[]): boolean {
  const init = call[1] as RequestInit | undefined;
  const path = new URL(String(call[0]), "http://local").pathname;
  return path in MOUNT_READS && (init?.method ?? "GET") === "GET";
}

function mockFetch(...responses: Partial<Response>[]) {
  const queue = [...responses];
  const fetchMock = vi.fn(async (...call: unknown[]) => {
    if (isMountRead(call)) {
      const body = MOUNT_READS[new URL(String(call[0]), "http://local").pathname];
      return { ok: true, status: 200, json: async () => body };
    }
    return queue.shift();
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The requests the form itself made, without the intent log's and sample roles' reads. */
function formCalls(fetchMock: ReturnType<typeof mockFetch>): unknown[][] {
  return fetchMock.mock.calls.filter((call) => !isMountRead(call));
}

async function renderLoaded(...rest: Partial<Response>[]) {
  const fetchMock = mockFetch(loaded, companyListConfig, ...rest);
  render(<PreferencesPage />);
  await screen.findByRole("heading", { name: "What you are looking for" });
  return fetchMock;
}

function chipField(label: string) {
  return screen.getByLabelText(label).closest(".chip-field") as HTMLElement;
}

/** Most of the form is collapsed on arrival, so a test that touches a control has to open
 * its section first — exactly as a user does. */
function expand(title: string) {
  fireEvent.click(screen.getByRole("button", { name: title, expanded: false }));
}

function bodyOf(call: unknown[]): Record<string, unknown> {
  return JSON.parse((call[1] as RequestInit).body as string) as Record<string, unknown>;
}

describe("preferences screen", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("marks itself as the current step and loads the saved values", async () => {
    await renderLoaded();
    expect(screen.getByText("PREFERENCES")).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("heading", { name: "What should it look for?" })).toBeInTheDocument();
    expand("More role rules");
    expect(within(chipField("Skills & stack")).getByText("PYTHON")).toBeInTheDocument();
    expand("Notes");
    expect(screen.getByRole("textbox", { name: "Notes" })).toHaveValue(
      "Munich only with a Berlin office.",
    );
  });

  it("opens as a short page: the tuning is behind its own headings", async () => {
    await renderLoaded();
    // The one field in each section a user actually fills is in the open.
    expect(await screen.findByLabelText("Add to what you are looking for")).toBeVisible();
    expect(screen.getByLabelText("Allowed locations")).toBeVisible();
    // The structured form of the same answer is tuning, so it is not.
    expect(screen.getByLabelText("Role families")).not.toBeVisible();
    // Everything else is a heading and nothing more until asked.
    expect(screen.queryByRole("textbox", { name: "Notes" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "staff" })).not.toBeInTheDocument();
    for (const title of [
      "More role rules",
      "More location rules",
      "Seniority",
      "Languages",
      "Employment & run size",
      "Notes",
    ]) {
      expect(screen.getByRole("button", { name: title })).toHaveAttribute(
        "aria-expanded",
        "false",
      );
    }
  });

  it("explains a section behind its ⓘ rather than on the page", async () => {
    await renderLoaded();
    const sentence = /A posting demanding German above it is rejected/;
    expect(screen.queryByText(sentence)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "About Languages" }));
    expect(screen.getByRole("note")).toHaveTextContent(sentence);
  });

  it("opens the section holding a refused field so its message is not hidden", async () => {
    await renderLoaded({
      ok: false,
      status: 422,
      json: async () => ({ detail: { errors: { core_skills: "name at least one skill" } } }),
    });
    fireEvent.click(screen.getByRole("button", { name: "Save preferences" }));

    expect(await screen.findByText("name at least one skill")).toBeVisible();
    expect(screen.getByRole("button", { name: "More role rules" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("badges values that came from the resume", async () => {
    await renderLoaded();
    const kafka = within(chipField("Skills & stack")).getByText("KAFKA").closest("li");
    expect(within(kafka as HTMLElement).getByText("RESUME")).toBeInTheDocument();
    const python = within(chipField("Skills & stack")).getByText("PYTHON").closest("li");
    expect(within(python as HTMLElement).queryByText("RESUME")).not.toBeInTheDocument();
  });

  it("makes Continue the primary action and Save the secondary one", async () => {
    mockFetch(
      { ok: true, status: 200, json: async () => ({ ...preferences, version: 1 }) },
      companyListConfig,
    );
    render(<PreferencesPage />);
    const save = await screen.findByRole("button", { name: "Save preferences" });
    const next = screen.getByRole("link", { name: "Continue to Recommendations" });
    expect(next).toHaveClass("primary-action");
    expect(save).toHaveClass("secondary-action");
    expect(save).not.toHaveClass("primary-action");
    expect(next).toHaveAttribute("href", "/recommendations");
  });

  it("offers no Continue link while the version is zero", async () => {
    await renderLoaded();
    expect(screen.queryByRole("link", { name: /Continue to/ })).not.toBeInTheDocument();
  });

  it("says it has never been saved while the version is zero", async () => {
    await renderLoaded();
    expect(
      screen.getByText(
        "These preferences have never been saved; they come from your config file and resume.",
      ),
    ).toBeInTheDocument();
  });

  it("reports an unreachable service instead of an empty form", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(new TypeError("Failed to fetch")));
    render(<PreferencesPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(PREFERENCES_UNREACHABLE);
  });

  // --- chips --------------------------------------------------------------

  it("adds a typed skill as a lowercase term", async () => {
    const fetchMock = await renderLoaded(saved());
    expand("More role rules");
    const input = within(chipField("Skills & stack")).getByRole("textbox");
    fireEvent.change(input, { target: { value: "  Apache  Kafka " } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Save preferences" }));

    await waitFor(() => expect(formCalls(fetchMock)).toHaveLength(3));
    const skills = bodyOf(formCalls(fetchMock)[2]).coreSkills as { term: string }[];
    expect(skills.map((skill) => skill.term)).toContain("apache kafka");
  });

  it("removes a chip", async () => {
    await renderLoaded();
    expand("More role rules");
    fireEvent.click(screen.getByRole("button", { name: "Remove JAVA" }));
    expect(within(chipField("Excluded stack")).queryByText("JAVA")).not.toBeInTheDocument();
  });

  it("stops at five role families and says so", async () => {
    await renderLoaded();
    expand("More role rules");
    const field = chipField("Role families");
    const input = within(field).getByRole("textbox");
    for (const role of ["backend", "data", "platform", "research"]) {
      fireEvent.change(input, { target: { value: role } });
      fireEvent.keyDown(input, { key: "Enter" });
    }
    expect(within(field).getByText("5 of 5")).toBeInTheDocument();
    expect(input).toBeDisabled();
    expect(within(field).getByRole("button", { name: "ADD" })).toBeDisabled();
  });

  it("toggles a seniority band", async () => {
    const fetchMock = await renderLoaded(saved());
    expand("Seniority");
    const staff = screen.getByRole("button", { name: "staff" });
    expect(staff).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(staff);
    expect(staff).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", { name: "Save preferences" }));
    await waitFor(() => expect(formCalls(fetchMock)).toHaveLength(3));
    expect(bodyOf(formCalls(fetchMock)[2]).seniority).toEqual(["mid", "senior", "staff"]);
  });

  it("changes a language level", async () => {
    const fetchMock = await renderLoaded(saved());
    expand("Languages");
    fireEvent.change(screen.getByLabelText("German level"), { target: { value: "C1" } });
    fireEvent.click(screen.getByRole("button", { name: "Save preferences" }));

    await waitFor(() => expect(formCalls(fetchMock)).toHaveLength(3));
    const languages = bodyOf(formCalls(fetchMock)[2]).languages as { name: string; level: string }[];
    expect(languages).toContainEqual(expect.objectContaining({ name: "German", level: "C1" }));
  });

  // --- saving -------------------------------------------------------------

  it("reports what the save re-filtered and what it re-opened", async () => {
    await renderLoaded(saved());
    fireEvent.click(screen.getByRole("button", { name: "Save preferences" }));
    const report = await screen.findByText(/Saved as version 1\./, {
      selector: ".save-report",
    });
    expect(report).toHaveTextContent(
      "Re-filtered 12 stored jobs: 3 reject→pass. 3 would be scored on the next run.",
    );
  });

  it("marks the offending fields when the server refuses the form", async () => {
    await renderLoaded({
      ok: false,
      status: 422,
      json: async () => ({
        detail: { errors: { allowed_cities: "list at least one location", batch_size: "too small" } },
      }),
    });
    fireEvent.click(screen.getByRole("button", { name: "Save preferences" }));
    expect(await screen.findByText("list at least one location")).toBeInTheDocument();
    expect(screen.getByText("too small")).toBeInTheDocument();
  });

  // --- notes extraction ---------------------------------------------------

  const proposal = {
    ok: true,
    status: 200,
    json: async () => ({
      changes: [
        {
          field: "flagged_cities",
          action: "add",
          value: "hamburg",
          label: "Hamburg",
          rationale: "the note mentions Hamburg",
        },
      ],
      costEur: 0.0123,
    }),
  };

  it("states the cost before extracting and shows the rationale after", async () => {
    await renderLoaded(proposal);
    expand("Notes");
    expect(
      screen.getByText(/Makes one paid model call\./),
    ).toHaveTextContent("Nothing is changed until you accept a suggestion");

    fireEvent.click(screen.getByRole("button", { name: "EXTRACT INTO PREFERENCES" }));
    expect(await screen.findByText("Add Hamburg to flagged cities")).toBeInTheDocument();
    expect(screen.getByText("the note mentions Hamburg")).toBeInTheDocument();
    expect(screen.getByText(/€0\.0123/)).toBeInTheDocument();
  });

  it("extracts against unsaved form edits", async () => {
    const fetchMock = await renderLoaded(proposal);
    expand("Languages");
    expand("Notes");
    fireEvent.change(screen.getByRole("combobox", { name: "German level" }), {
      target: { value: "C1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "EXTRACT INTO PREFERENCES" }));
    await screen.findByRole("button", { name: "ACCEPT" });
    const draft = bodyOf(formCalls(fetchMock)[2]).preferences as Preferences;
    expect(draft.languages.find((language) => language.name === "German")?.level).toBe("C1");
  });

  it("applies a change only when it is accepted", async () => {
    await renderLoaded(proposal);
    expand("Notes");
    fireEvent.click(screen.getByRole("button", { name: "EXTRACT INTO PREFERENCES" }));
    fireEvent.click(await screen.findByRole("button", { name: "ACCEPT" }));

    expect(within(chipField("Flagged")).getByText("Hamburg")).toBeInTheDocument();
    expect(screen.queryByText("Add Hamburg to flagged cities")).not.toBeInTheDocument();
  });

  it("drops a rejected change without touching the form", async () => {
    await renderLoaded(proposal);
    expand("Notes");
    fireEvent.click(screen.getByRole("button", { name: "EXTRACT INTO PREFERENCES" }));
    fireEvent.click(await screen.findByRole("button", { name: "REJECT" }));

    expect(within(chipField("Flagged")).queryByText("Hamburg")).not.toBeInTheDocument();
    expect(screen.queryByText("Add Hamburg to flagged cities")).not.toBeInTheDocument();
  });

  it("accepting a proposal saves nothing on its own", async () => {
    const fetchMock = await renderLoaded(proposal);
    expand("Notes");
    fireEvent.click(screen.getByRole("button", { name: "EXTRACT INTO PREFERENCES" }));
    fireEvent.click(await screen.findByRole("button", { name: "ACCEPT" }));
    // The preferences mount read, the company-list summary mount read, and the
    // extraction call — nothing else beyond the intent log's and sample roles' reads.
    expect(formCalls(fetchMock)).toHaveLength(3);
    expect(fetchMock.mock.calls.every((call) => (call[1] as RequestInit)?.method !== "PUT")).toBe(
      true,
    );
  });

  it("explains a missing model key rather than failing silently", async () => {
    await renderLoaded({ ok: false, status: 409 });
    expand("Notes");
    fireEvent.click(screen.getByRole("button", { name: "EXTRACT INTO PREFERENCES" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(NO_MODEL_KEY);
  });

  describe("filter impact preview", () => {
    function previewResponse() {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          examined: 40,
          changed: 3,
          transitions: { "pass→reject": 3 },
          nowPassing: 0,
          hidden: 3,
          excludedRoleTerms: {
            manager: { matches: 5, newlyHidden: 3 },
            frontend: { matches: 0, newlyHidden: 0 },
          },
        }),
      };
    }

    function addExcludedTerm(value: string) {
      expand("More role rules");
      const input = within(chipField("Excluded role terms")).getByRole("textbox");
      fireEvent.change(input, { target: { value } });
      fireEvent.keyDown(input, { key: "Enter" });
    }

    it("shows what an excluded term would hide before anything is saved", async () => {
      const fetchMock = await renderLoaded(previewResponse());
      addExcludedTerm("manager");

      expect(
        await screen.findByText(/is in 5 stored job titles; 3 would be newly hidden/),
      ).toBeInTheDocument();
      expect(
        screen.getByText(/Saving would re-filter 40 stored jobs: 3 pass→reject/),
      ).toBeInTheDocument();
      // A term that matches nothing says nothing.
      expect(screen.queryByText("frontend", { selector: "strong" })).not.toBeInTheDocument();
      const call = formCalls(fetchMock).find((c) =>
        String(c[0]).endsWith("/api/preferences/preview"),
      );
      expect(call).toBeDefined();
      const sent = bodyOf(call as unknown[]).excludedRoleTerms as { term: string }[];
      expect(sent.map((t) => t.term)).toContain("manager");
    });

    it("asks for no preview when nothing a filter reads has changed", async () => {
      const fetchMock = await renderLoaded();
      expand("Notes");
      fireEvent.change(screen.getByRole("textbox", { name: "Notes" }), {
        target: { value: "new note" },
      });
      await new Promise((resolve) => setTimeout(resolve, 600));

      expect(formCalls(fetchMock).some((c) => String(c[0]).endsWith("/preview"))).toBe(false);
    });

    it("goes quiet again when the draft is put back as it was saved", async () => {
      await renderLoaded(previewResponse());
      addExcludedTerm("manager");
      await screen.findByText(/Saving would re-filter/);

      fireEvent.click(screen.getByRole("button", { name: "Remove manager" }));

      await waitFor(() => expect(screen.queryByText(/Saving would/)).not.toBeInTheDocument());
    });
  });
});
