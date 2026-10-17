import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CompanySources } from "@/components/company-sources";

function mockFetch(...responses: Partial<Response>[]) {
  const fetchMock = vi.fn();
  for (const response of responses) fetchMock.mockResolvedValueOnce(response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const config = (overrides: object = {}) => ({
  ok: true,
  status: 200,
  json: async () => ({
    notion: { databaseId: "" },
    csv: { path: "" },
    configured: null,
    notionKeyConfigured: false,
    ...overrides,
  }),
});

const notionConfigured = config({
  notion: { databaseId: "abc123" },
  configured: "notion",
  notionKeyConfigured: true,
});

describe("company sources", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("loads the current connection and reports whether anything is configured", async () => {
    const onConfiguredChange = vi.fn();
    mockFetch(notionConfigured);
    render(<CompanySources onConfiguredChange={onConfiguredChange} />);
    expect(await screen.findByDisplayValue("abc123")).toBeInTheDocument();
    // The detected marker sits beside the Notion label, not under the CSV upload.
    const row = screen.getByText("Notion database ID").closest(".source-label-row") as HTMLElement;
    expect(row).toHaveTextContent("✓ Detected — included when you continue");
    expect(onConfiguredChange).toHaveBeenLastCalledWith("notion");
  });

  it("labels the Notion field in plain sight, with no marker until one is saved", async () => {
    mockFetch(config());
    render(<CompanySources />);
    expect(await screen.findByText("Notion database ID")).toBeVisible();
    expect(screen.queryByText(/Detected/)).not.toBeInTheDocument();
  });

  it("has no sync button: connecting only saves the connection", async () => {
    mockFetch(notionConfigured);
    render(<CompanySources />);
    await screen.findByDisplayValue("abc123");
    expect(screen.queryByRole("button", { name: /sync/i })).not.toBeInTheDocument();
  });

  it("saves a database id and says it syncs on continue", async () => {
    const onConfiguredChange = vi.fn();
    const fetchMock = mockFetch(config(), notionConfigured);
    render(<CompanySources onConfiguredChange={onConfiguredChange} />);
    await screen.findByLabelText("Notion database ID");
    expect(onConfiguredChange).toHaveBeenLastCalledWith(null);

    fireEvent.change(screen.getByLabelText("Notion database ID"), { target: { value: "abc123" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    const [, call] = fetchMock.mock.calls;
    expect(call[0]).toContain("/api/company-lists/config");
    expect((call[1] as RequestInit).method).toBe("PUT");
    expect(onConfiguredChange).toHaveBeenLastCalledWith("notion");
  });

  it("keeps the long explanations behind an ⓘ until asked", async () => {
    mockFetch(notionConfigured);
    render(<CompanySources />);
    await screen.findByDisplayValue("abc123");
    expect(screen.queryByText(/matched to its public job board/)).not.toBeInTheDocument();
    expect(screen.queryByText(/never sent to this page/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "About company sources" }));
    expect(screen.getByText(/matched to its public job board/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "About the Notion connection" }));
    expect(screen.getByText(/never sent to this page/)).toBeInTheDocument();
  });

  it("only mentions the token when it is missing", async () => {
    mockFetch(config({ notion: { databaseId: "abc123" }, configured: "notion" }));
    render(<CompanySources />);
    expect(await screen.findByText("Add NOTION_API_KEY to .env to use Notion.")).toBeInTheDocument();
  });

  it("keeps the CSV upload out of the way until it is asked for", async () => {
    mockFetch(config());
    render(<CompanySources />);
    // Notion is the path almost everyone takes, so it stays in plain sight.
    expect(await screen.findByText("Notion database ID")).toBeVisible();
    const toggle = screen.getByRole("button", { name: "Upload a CSV list instead" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByLabelText("Drop a company-list CSV here")).not.toBeVisible();

    fireEvent.click(toggle);
    expect(screen.getByLabelText("Drop a company-list CSV here")).toBeVisible();
  });

  it("opens the CSV upload itself when one fails, so the reason is not hidden", async () => {
    mockFetch(config());
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError("Failed to fetch"));
    render(<CompanySources />);
    const input = (await screen.findByLabelText("Drop a company-list CSV here")) as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File(["name\nAcme"], "list.csv")] } });

    const failure = await screen.findByRole("alert");
    expect(failure).toBeVisible();
    expect(screen.getByRole("button", { name: "Upload a CSV list instead" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("uploads a CSV and reports how many companies it holds", async () => {
    const csvConfigured = config({ csv: { path: "data/company_list.csv" }, configured: "csv" });
    const fetchMock = mockFetch(
      config(),
      { ok: true, status: 201, json: async () => ({ path: "data/company_list.csv", companies: 3 }) },
      csvConfigured,
    );
    render(<CompanySources />);
    const input = (await screen.findByLabelText("Drop a company-list CSV here")) as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File(["name\nAcme"], "list.csv")] } });

    expect(await screen.findByText("Uploaded 3 companies.")).toBeInTheDocument();
    expect(fetchMock.mock.calls[1][0]).toContain("/api/company-lists/csv");
    const row = screen.getByText("CSV list").closest(".source-label-row") as HTMLElement;
    expect(row).toHaveTextContent("✓ Detected — included when you continue");
  });

  it("reports a config load failure instead of an empty panel", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValueOnce(new TypeError("Failed to fetch")));
    render(<CompanySources />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not reach the company-list service. Is the API running?",
    );
  });
});
