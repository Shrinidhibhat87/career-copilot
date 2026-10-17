import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AddCompanyBoard } from "@/components/add-company-board";

const ok = (body: unknown, status = 200) =>
  ({ ok: true, status, json: async () => body }) as Response;
const failed = (status: number, detail: unknown) =>
  ({ ok: false, status, json: async () => ({ detail }) }) as Response;

const found = {
  name: "Helsing",
  found: true,
  boardType: "ashby",
  slug: "helsing",
  openRoles: 12,
  tried: ["helsing"],
  blockedReason: null,
  canSearchWeb: false,
};
const missing = {
  name: "Zzyzx",
  found: false,
  boardType: null,
  slug: null,
  openRoles: null,
  tried: ["zzyzx", "zzyzxgmbh"],
  blockedReason: null,
  canSearchWeb: true,
};

async function find(name: string) {
  await userEvent.type(screen.getByLabelText("Company name"), name);
  await userEvent.click(screen.getByRole("button", { name: "Find job board" }));
}

describe("add a company by name", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("shows the board it found and adds it only after the user confirms", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      void init;
      if (String(input).includes("/lookup/confirm")) {
        return ok({ boardType: "ashby", slug: "helsing", openRoles: 12 }, 201);
      }
      return ok(found);
    });
    vi.stubGlobal("fetch", fetchMock);
    const onAdded = vi.fn();
    render(<AddCompanyBoard onAdded={onAdded} />);

    await find("Helsing");
    expect(await screen.findByText("ashby/helsing")).toBeInTheDocument();
    expect(onAdded).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1); // looked up, nothing added yet

    await userEvent.click(screen.getByRole("button", { name: "Add this board" }));
    expect(await screen.findByText(/Added ashby\/helsing/)).toBeInTheDocument();
    const [, init] = fetchMock.mock.calls[1];
    expect(JSON.parse(init?.body as string)).toEqual({
      boardType: "ashby",
      slug: "helsing",
      name: "Helsing",
    });
    expect(onAdded).toHaveBeenCalledTimes(1);
  });

  it("says a board that is already listed was left alone, and does not refresh the donut", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/lookup/confirm")
          ? ok(
              { boardType: "ashby", slug: "helsing", openRoles: null, alreadyListed: true, filledListEntry: null },
              201,
            )
          : ok(found),
      ),
    );
    const onAdded = vi.fn();
    render(<AddCompanyBoard onAdded={onAdded} />);

    await find("Helsing");
    await userEvent.click(await screen.findByRole("button", { name: "Add this board" }));

    expect(await screen.findByText(/ashby\/helsing is already in your list/)).toBeInTheDocument();
    expect(onAdded).not.toHaveBeenCalled();
  });

  it("names the not-found list entry a board was given to", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/lookup/confirm")
          ? ok(
              { boardType: "ashby", slug: "helsing", openRoles: 12, alreadyListed: false, filledListEntry: "Helsing GmbH" },
              201,
            )
          : ok(found),
      ),
    );
    const onAdded = vi.fn();
    render(<AddCompanyBoard onAdded={onAdded} />);

    await find("Helsing");
    await userEvent.click(await screen.findByRole("button", { name: "Add this board" }));

    expect(await screen.findByText(/for Helsing GmbH, which was not found before/)).toBeInTheDocument();
    expect(onAdded).toHaveBeenCalledTimes(1);
  });

  it("offers the web search only after a miss, and shows what it finds", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/lookup/search")) {
        return ok({
          name: "Zzyzx",
          boards: [
            {
              boardType: "lever",
              slug: "zzyzx",
              company: "Zzyzx",
              openRoles: 3,
              sampleTitles: ["Forward Deployed Engineer"],
              evidence: "careers page",
            },
          ],
        });
      }
      return ok(missing);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AddCompanyBoard />);

    await find("Zzyzx");
    await userEvent.click(await screen.findByRole("button", { name: "Search the web for it" }));

    expect(await screen.findByText("lever/zzyzx")).toBeInTheDocument();
    expect(screen.getByText(/Forward Deployed Engineer/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/lookup/confirm"))).toBe(
      false,
    );
  });

  it("does not offer a web search when the boards could not be checked", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        ok({ ...missing, canSearchWeb: false, blockedReason: "greenhouse/zzyzx: HTTP 429" }),
      ),
    );
    render(<AddCompanyBoard />);

    await find("Zzyzx");
    expect(await screen.findByText(/Try again in a minute/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Search the web for it" })).not.toBeInTheDocument();
  });

  it("shows why a wrong board was refused", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/lookup/confirm")
          ? failed(422, { errors: { board: "ashby has no board called 'helsing'. Nothing was changed." } })
          : ok(found),
      ),
    );
    render(<AddCompanyBoard />);

    await find("Helsing");
    await userEvent.click(await screen.findByRole("button", { name: "Add this board" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/no board called/));
  });
});
