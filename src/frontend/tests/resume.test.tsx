import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ResumePage from "@/app/resume/page";
import { UNREACHABLE } from "@/lib/api";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

const detections = {
  filename: "anna-schmidt-cv.pdf",
  parsedAt: "2026-08-22T10:00:00+00:00",
  version: 1,
  sha256: "0".repeat(64),
  changed: true,
  summary: { roles: 3, skills: 2, languages: 2 },
  privacy: {
    enabled: true,
    counts: { PERSON: 3, EMAIL: 1, PHONE: 1 },
    redactedPreview: "Data & AI Engineer · <|PERSON_1|> · Berlin",
    ownerAnchor: "confirmed" as const,
  },
  detected: {
    roles: ["AI/ML Engineering"],
    skills: ["Python", "FastAPI"],
    languages: [{ name: "English", level: "C1" as const }],
  },
};

// Once a resume is parsed the page also shows the company-source panel, which reads its
// own connection state; everything after the scripted responses answers that read.
const emptySources = {
  ok: true,
  status: 200,
  json: async () => ({
    notion: { databaseId: "" },
    csv: { path: "" },
    configured: null,
    notionKeyConfigured: false,
  }),
};

const isSources = (url: unknown) => String(url).includes("/api/company-lists");

// Scripted responses answer the resume calls in order; the company-source panel's own
// reads are answered separately so they never consume a resume response.
function mockFetch(...responses: Partial<Response>[]) {
  const queue = [...responses];
  const fetchMock = vi.fn((url: unknown, init?: RequestInit) => {
    void init;
    return Promise.resolve(isSources(url) ? emptySources : queue.shift());
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const resumeCalls = (fetchMock: ReturnType<typeof vi.fn>) =>
  fetchMock.mock.calls.filter((call) => !isSources(call[0]));

const notFound = { ok: false, status: 404 };
const parsed = { ok: true, status: 200, json: async () => detections };

// The dropzone appears only once the mount read has answered 404.
async function chooseFile(name = "cv.pdf") {
  const input = (await screen.findByLabelText(
    "Drop a PDF or DOCX here",
  )) as HTMLInputElement;
  fireEvent.change(input, { target: { files: [new File(["x"], name)] } });
}

describe("resume upload", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    push.mockClear();
    window.sessionStorage.clear();
  });

  it("renders the empty state and asks the server once on mount", async () => {
    const fetchMock = mockFetch(notFound);
    render(<ResumePage />);
    expect(
      screen.getByRole("heading", { name: "Start with your resume." }),
    ).toBeInTheDocument();
    // The file *is* POSTed to the local API, so the old "never uploaded" claim was false.
    expect(
      await screen.findByText(
        "max 5 MB · read by the local API on this machine · redacted before any model call",
      ),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Choose a file" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Set preferences manually")).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][1]).toBeUndefined();
  });

  it("restores an already-parsed resume on mount", async () => {
    mockFetch(parsed);
    render(<ResumePage />);
    expect(await screen.findByText("anna-schmidt-cv.pdf")).toBeInTheDocument();
    expect(
      screen.getByText(
        "version 1 · parsed · 3 roles · 2 skills · 2 languages detected",
      ),
    ).toBeInTheDocument();
    for (const skill of detections.detected.skills) {
      expect(screen.getByText(skill)).toBeInTheDocument();
    }
    expect(
      screen.getByRole("button", { name: /Review preferences/ }),
    ).toBeEnabled();
    // The disclosure travels with the parse result, not on a screen of its own.
    expect(
      screen.getByText(
        "3 names, 1 email address and 1 phone number will be replaced before any model call.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("<|PERSON_1|>")).toBeInTheDocument();
  });

  it("shows no privacy badge until a resume has been parsed", async () => {
    mockFetch(notFound);
    render(<ResumePage />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Choose a file" }),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("Redacted text")).not.toBeInTheDocument();
  });

  it("shows the uploading state while parsing, then the parsed state", async () => {
    let release: (value: Partial<Response>) => void = () => {};
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(notFound)
      .mockReturnValueOnce(
        new Promise<Partial<Response>>((resolve) => {
          release = resolve;
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    render(<ResumePage />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await chooseFile();
    expect(
      await screen.findByRole("button", { name: "Parsing…" }),
    ).toBeDisabled();
    release(parsed);
    expect(await screen.findByText("anna-schmidt-cv.pdf")).toBeInTheDocument();
  });

  it.each([
    [415, "Only PDF and DOCX files are supported."],
    [413, "That file is over the 5 MB limit."],
    [422, "Could not read any text from that file."],
    [502, "Something went wrong reading that file."],
  ])(
    "reports %i inline and leaves the input usable",
    async (status, message) => {
      mockFetch(notFound, { ok: false, status });
      render(<ResumePage />);
      await chooseFile("notes.txt");
      expect(await screen.findByRole("alert")).toHaveTextContent(message);
      const input = screen.getByLabelText("Drop a PDF or DOCX here");
      expect(input).not.toBeDisabled();
      expect(input).toHaveAttribute("aria-describedby", "resume-error");
    },
  );

  it("removes the resume and returns to the empty state", async () => {
    const fetchMock = mockFetch(parsed, { ok: true, status: 204 });
    render(<ResumePage />);
    fireEvent.click(await screen.findByRole("button", { name: "REMOVE" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Choose a file" }),
      ).toBeInTheDocument(),
    );
    expect(resumeCalls(fetchMock)[1][1]).toMatchObject({ method: "DELETE" });
  });

  it("shows a skeleton, not the dropzone, while the CV is loading", async () => {
    let release: (value: Partial<Response>) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn((url: unknown) =>
        isSources(url)
          ? Promise.resolve(emptySources)
          : new Promise<Partial<Response>>((resolve) => {
              release = resolve;
            }),
      ),
    );
    render(<ResumePage />);
    expect(screen.getByTestId("resume-skeleton")).toBeInTheDocument();
    expect(screen.queryByText("Drop a PDF or DOCX here")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Choose a file" })).not.toBeInTheDocument();

    release(notFound);
    expect(await screen.findByText("Drop a PDF or DOCX here")).toBeInTheDocument();
    expect(screen.queryByTestId("resume-skeleton")).not.toBeInTheDocument();
  });

  it("never shows the dropzone when the mount read fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce({ ok: false, status: 500 }));
    render(<ResumePage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(UNREACHABLE);
    expect(screen.queryByText("Drop a PDF or DOCX here")).not.toBeInTheDocument();
  });

  it("says so when the mount read cannot reach the service", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValueOnce(new TypeError("Failed to fetch")),
    );
    render(<ResumePage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(UNREACHABLE);
    // Not a 404, so the page must not claim there is no CV by offering the dropzone.
    expect(screen.queryByRole("button", { name: "Choose a file" })).not.toBeInTheDocument();
  });

  it("does not claim absence when the mount read fails with a server error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValueOnce({ ok: false, status: 500 }),
    );
    render(<ResumePage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(UNREACHABLE);
  });

  it("keeps the resume on screen when removing it fails", async () => {
    mockFetch(parsed, { ok: false, status: 500 });
    render(<ResumePage />);
    fireEvent.click(await screen.findByRole("button", { name: "REMOVE" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(UNREACHABLE);
    expect(screen.getByText("anna-schmidt-cv.pdf")).toBeInTheDocument();
    // The resume dropzone stays hidden; only the company-list CSV one remains.
    expect(
      screen.queryByLabelText("Drop a PDF or DOCX here"),
    ).not.toBeInTheDocument();
  });

  it("offers the company sources only once a resume is parsed", async () => {
    mockFetch(notFound);
    render(<ResumePage />);
    await screen.findByRole("button", { name: "Choose a file" });
    expect(
      screen.queryByText("Companies you are targeting"),
    ).not.toBeInTheDocument();
  });

  it("continues straight to preferences, without a sync, when no source is connected", async () => {
    const fetchMock = mockFetch(parsed);
    render(<ResumePage />);
    await screen.findByText("Companies you are targeting");
    fireEvent.click(screen.getByRole("button", { name: /Review preferences/ }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/preferences"));
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/sync")),
    ).toBe(false);
  });

  it("starts the sync on Continue, remembers its run, then goes to preferences", async () => {
    const connected = {
      ok: true,
      status: 200,
      json: async () => ({
        notion: { databaseId: "abc123" },
        csv: { path: "" },
        configured: "notion",
        notionKeyConfigured: true,
      }),
    };
    const queue = [parsed];
    const fetchMock = vi.fn((url: unknown, init?: RequestInit) => {
      if (
        String(url).endsWith("/api/company-lists/sync") &&
        init?.method === "POST"
      ) {
        return Promise.resolve({
          ok: true,
          status: 201,
          json: async () => ({ runId: 7, state: "syncing" }),
        });
      }
      return Promise.resolve(isSources(url) ? connected : queue.shift());
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ResumePage />);
    await screen.findByDisplayValue("abc123");

    // Connecting is not syncing: nothing has been started yet.
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/sync")),
    ).toBe(false);
    // The detected source is named next to the button before anything is clicked.
    expect(
      screen.getByText(/✓ Detected — included when you continue/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Review preferences/ }));
    expect(
      await screen.findByRole("button", { name: "Syncing your Notion list…" }),
    ).toBeDisabled();

    await waitFor(() => expect(push).toHaveBeenCalledWith("/preferences"));
    expect(window.sessionStorage.getItem("companySyncRunId")).toBe("7");
  });

  it("stays on the page and says why when the sync cannot start", async () => {
    const connected = {
      ok: true,
      status: 200,
      json: async () => ({
        notion: { databaseId: "abc123" },
        csv: { path: "" },
        configured: "notion",
        notionKeyConfigured: false,
      }),
    };
    const queue = [parsed];
    const fetchMock = vi.fn((url: unknown, init?: RequestInit) => {
      if (
        String(url).endsWith("/api/company-lists/sync") &&
        init?.method === "POST"
      ) {
        return Promise.resolve({
          ok: false,
          status: 409,
          json: async () => ({
            detail: { errors: { notion: "set NOTION_API_KEY in .env first" } },
          }),
        });
      }
      return Promise.resolve(isSources(url) ? connected : queue.shift());
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ResumePage />);
    await screen.findByDisplayValue("abc123");
    fireEvent.click(screen.getByRole("button", { name: /Review preferences/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /NOTION_API_KEY/,
    );
    expect(push).not.toHaveBeenCalled();
    expect(
      screen.getByRole("link", { name: "Continue without syncing" }),
    ).toHaveAttribute("href", "/preferences");
  });
});
