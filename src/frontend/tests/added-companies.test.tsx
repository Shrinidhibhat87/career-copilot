import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AddedCompanies } from "@/components/added-companies";

const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as Response;

const boards = [
  {
    name: "Helsing",
    boardType: "ashby",
    slug: "helsing",
    origin: "manual",
    status: "resolved",
    addedAt: "2026-10-01T09:00:00+00:00",
    openRoles: 12,
    lastVisitedAt: null,
  },
  {
    name: "Acme Corp",
    boardType: "greenhouse",
    slug: "acme",
    origin: "imported",
    status: "resolved",
    addedAt: "2026-09-01T09:00:00+00:00",
    openRoles: 0,
    lastVisitedAt: "2026-10-10T09:00:00+00:00",
  },
];

describe("added companies", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("lists every board in the order given, and says when one was never visited", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ok(boards)));
    render(<AddedCompanies />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "View added companies" }));

    const dialog = await screen.findByRole("dialog", { name: "Added companies" });
    const rows = (await within(dialog).findAllByRole("row")).slice(1);
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("Helsing")).toBeInTheDocument();
    expect(within(rows[0]).getByText("ashby/helsing")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Added by you")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Never")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Acme Corp")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Company list")).toBeInTheDocument();
    expect(within(rows[1]).queryByText("Never")).not.toBeInTheDocument();
  });

  it("closes with Escape and with the Close button", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ok(boards)));
    render(<AddedCompanies />);

    await userEvent.click(screen.getByRole("button", { name: "View added companies" }));
    await screen.findByRole("dialog");
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "View added companies" }));
    await userEvent.click(await screen.findByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("reports a failed load instead of an empty table", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) }) as Response),
    );
    render(<AddedCompanies />);

    await userEvent.click(screen.getByRole("button", { name: "View added companies" }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});
