import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { UnfoundCompanies } from "@/components/unfound-companies";
import type { CompanyListSummary } from "@/lib/api";

type Company = CompanyListSummary["companies"][number];

const company = (overrides: Partial<Company>): Company => ({
  id: 1,
  name: "N26 Bank",
  pinned: false,
  status: "unresolved",
  boardType: null,
  openRoles: null,
  lastError: "no public board found (tried: n26-bank)",
  ...overrides,
});

const openSection = () => userEvent.click(screen.getByRole("button", { name: /^\d+ compan/ }));

describe("companies not found", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("renders nothing when every company was found", () => {
    const { container } = render(
      <UnfoundCompanies
        companies={[company({ status: "resolved", lastError: null })]}
        onResolved={() => {}}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("counts unresolved and blocked companies and lists each with its reason", async () => {
    render(
      <UnfoundCompanies
        companies={[
          company({}),
          company({ id: 2, name: "Slow Corp", status: "blocked", lastError: "greenhouse/slow: HTTP 429" }),
          company({ id: 3, name: "Esri", status: "resolved", lastError: null }),
        ]}
        onResolved={() => {}}
      />,
    );
    expect(screen.getByText(/2 companies not found/)).toBeInTheDocument();
    expect(screen.getByLabelText("N26 Bank")).toBeInTheDocument();
    expect(screen.getByLabelText("Slow Corp")).toBeInTheDocument();
    expect(screen.queryByLabelText("Esri")).not.toBeInTheDocument();
    expect(screen.getByText("greenhouse/slow: HTTP 429")).toBeInTheDocument();
  });

  it("sends the board the user typed and tells the page it was saved", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: 1, status: "resolved", boardType: "ashby", slug: "n26", openRoles: 12, pinned: true }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const onResolved = vi.fn();
    render(<UnfoundCompanies companies={[company({})]} onResolved={onResolved} />);

    await openSection();
    await userEvent.type(screen.getByLabelText("N26 Bank"), "ashby/n26");
    await userEvent.click(screen.getByRole("button", { name: "Check and save" }));

    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain("/api/company-lists/companies/1/board");
    expect(JSON.parse(init.body)).toEqual({ board: "ashby/n26" });
  });

  it("shows why a wrong answer was refused and keeps what was typed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: async () => ({ detail: { errors: { board: "ashby has no board called 'nope'. Nothing was changed." } } }),
      }),
    );
    const onResolved = vi.fn();
    render(<UnfoundCompanies companies={[company({})]} onResolved={onResolved} />);

    await openSection();
    await userEvent.type(screen.getByLabelText("N26 Bank"), "ashby/nope");
    await userEvent.click(screen.getByRole("button", { name: "Check and save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("no board called 'nope'");
    expect(screen.getByLabelText("N26 Bank")).toHaveValue("ashby/nope");
    expect(onResolved).not.toHaveBeenCalled();
  });

  it("will not submit an empty answer", async () => {
    render(<UnfoundCompanies companies={[company({})]} onResolved={() => {}} />);
    await openSection();
    expect(screen.getByRole("button", { name: "Check and save" })).toBeDisabled();
  });
});
