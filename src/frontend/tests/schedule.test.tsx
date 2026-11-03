import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ScheduleSection } from "@/components/schedule-section";
import type { ScheduleView } from "@/lib/api";
import { formatTime, nextLabel, to12, to24 } from "@/lib/schedule-time";

const respond = (body: unknown, status = 200) =>
  ({ ok: status < 400, status, json: async () => body }) as Response;

const SLOTS = [
  { time: "08:00", on: true },
  { time: "12:30", on: false },
  { time: "18:00", on: true },
  { time: "22:00", on: false },
];
const VIEW: ScheduleView = {
  enabled: false,
  times: [],
  slots: SLOTS,
  next: null,
  lastRunAt: null,
  lastSkip: null,
  canRun: true,
  whyNot: null,
};

function stub(view: ScheduleView = VIEW, put?: () => Response) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${init?.body ?? ""}`);
      return init?.method === "PUT" ? (put?.() ?? respond({ ...view, enabled: true })) : respond(view);
    }),
  );
  return calls;
}

/** The section rests collapsed, like the others on Settings. */
const open = () => fireEvent.click(screen.getByRole("button", { name: /^Schedule/ }));

beforeEach(() => vi.unstubAllGlobals());
afterEach(() => vi.unstubAllGlobals());

describe("time helpers", () => {
  it("round-trips the 24-hour and 12-hour clocks", () => {
    expect(to12("00:30")).toEqual({ hour: 12, minute: "30", pm: false });
    expect(to12("12:00")).toEqual({ hour: 12, minute: "00", pm: true });
    expect(to24({ hour: 12, minute: "00", pm: false })).toBe("00:00");
    expect(to24({ hour: 7, minute: "30", pm: true })).toBe("19:30");
    expect(formatTime("18:00")).toBe("06:00 PM");
    expect(formatTime("08:30")).toBe("08:30 AM");
  });

  it("words the next run relative to today", () => {
    const now = new Date(2030, 0, 10, 9, 0);
    expect(nextLabel(new Date(2030, 0, 10, 18, 0).toISOString(), now)).toMatch(/^Today at /);
    expect(nextLabel(new Date(2030, 0, 11, 8, 0).toISOString(), now)).toMatch(/^Tomorrow at /);
  });
});

describe("ScheduleSection", () => {
  it("is collapsed until it is opened", async () => {
    stub();
    render(<ScheduleSection />);
    expect(screen.queryByRole("button", { name: "Morning time" })).toBeNull();
    open();
    expect(await screen.findByRole("button", { name: "Morning time" })).toBeInTheDocument();
  });

  it("shows four named slots with their times and the off state", async () => {
    stub();
    render(<ScheduleSection />);
    open();
    expect(await screen.findByRole("button", { name: "Morning time" })).toHaveTextContent("08:00 AM");
    expect(screen.getByRole("button", { name: "Midday time" })).toHaveTextContent("12:30 PM");
    expect(screen.getByRole("button", { name: "Evening time" })).toHaveTextContent("06:00 PM");
    expect(screen.getByRole("button", { name: "Night time" })).toHaveTextContent("10:00 PM");
    expect(screen.getByRole("switch", { name: "Run daily" })).not.toBeChecked();
    expect(screen.getByRole("switch", { name: "Morning slot" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Midday slot" })).not.toBeChecked();
    expect(screen.getByText("Off")).toBeInTheDocument();
    expect(screen.getByText("Run when I open it")).toBeInTheDocument();
  });

  it("switching a slot off removes it from what is saved, and keeps its time", async () => {
    const calls = stub();
    render(<ScheduleSection />);
    open();
    fireEvent.click(await screen.findByRole("switch", { name: "Run daily" }));
    fireEvent.click(screen.getByRole("switch", { name: "Morning slot" }));
    fireEvent.click(screen.getByRole("button", { name: "Save schedule" }));
    expect(await screen.findByText(/Saved\./)).toBeInTheDocument();
    const body = JSON.parse(calls.find((c) => c.startsWith("PUT"))!.slice(4));
    expect(body.enabled).toBe(true);
    expect(body.slots[0]).toEqual({ time: "08:00", on: false });
  });

  it("picks a time on the wheel and saves it in 24-hour form", async () => {
    const calls = stub();
    render(<ScheduleSection />);
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Evening time" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(within(dialog).getByRole("listbox", { name: "Hour" })).getByRole("option", { name: "7" }));
    fireEvent.click(within(within(dialog).getByRole("listbox", { name: "Minute" })).getByRole("option", { name: "30" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Done" }));
    expect(screen.getByRole("button", { name: "Evening time" })).toHaveTextContent("07:30 PM");
    fireEvent.click(screen.getByRole("button", { name: "Save schedule" }));
    await screen.findByText(/Saved\./);
    expect(calls.find((c) => c.startsWith("PUT"))).toContain('{"time":"19:30","on":true}');
  });

  it("cancel keeps the old time", async () => {
    stub();
    render(<ScheduleSection />);
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Morning time" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(within(dialog).getByRole("listbox", { name: "AM or PM" })).getByRole("option", { name: "PM" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Morning time" })).toHaveTextContent("08:00 AM");
  });

  it("blocks saving while two slots that are on share a time", async () => {
    stub({ ...VIEW, slots: [SLOTS[0], { time: "08:00", on: true }, SLOTS[2], SLOTS[3]] });
    render(<ScheduleSection />);
    open();
    expect(await screen.findByRole("alert")).toHaveTextContent("same time");
    expect(screen.getByRole("button", { name: "Save schedule" })).toBeDisabled();
  });

  it("shows the next run, why a run would be skipped, and the last skip", async () => {
    stub({
      ...VIEW,
      enabled: true,
      next: new Date(Date.now() + 3_600_000).toISOString(),
      canRun: false,
      whyNot: "upload a CV first",
      lastSkip: { reason: "upload a CV first", at: "2030-01-01T08:00:00Z" },
    });
    render(<ScheduleSection />);
    open();
    expect(await screen.findByRole("status")).toHaveTextContent("skipped now: upload a CV first");
    expect(screen.getByText(/Last skip/)).toBeInTheDocument();
    expect(screen.getByText(/at \d\d:\d\d/)).toBeInTheDocument();
  });

  it("shows a refused save", async () => {
    stub(VIEW, () => respond({ detail: { errors: { slots: "switch on at least one slot" } } }, 422));
    render(<ScheduleSection />);
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Save schedule" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("switch on at least one slot");
  });
});
