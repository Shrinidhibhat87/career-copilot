import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CompanyDonut, CompanyDonutSkeleton } from "@/components/company-donut";
import { ringArcs, segmentCompanies, type CompanyEntry } from "@/lib/company-segments";

const company = (
  name: string,
  status: CompanyEntry["status"],
  openRoles: number | null = null,
  lastError: string | null = null,
): CompanyEntry => ({ id: 0, name, pinned: false, status, boardType: null, openRoles, lastError });

const companies: CompanyEntry[] = [
  company("Esri", "resolved", 447),
  company("Peec AI", "resolved", 35),
  company("Intrinsic", "resolved", 0),
  company("Ghost Co", "unresolved", null, "no public board found (tried: ghost-co, ghostcogmbh)"),
  company("Zendesk", "unresolved", null, "no public board found (tried: zendesk)"),
  company("Slow Corp", "blocked", null, "greenhouse/slow-corp: timed out"),
  company("Gone", "inactive"),
];

describe("segmentCompanies", () => {
  it("groups the list into the four states and leaves out companies no longer listed", () => {
    const segments = segmentCompanies(companies);
    expect(segments.map((s) => [s.key, s.count])).toEqual([
      ["withRoles", 2],
      ["noRoles", 1],
      ["blocked", 1],
      ["notFound", 2],
    ]);
  });

  it("puts the boards with the most roles first and sorts the rest by name", () => {
    const [withRoles, , , notFound] = segmentCompanies(companies);
    expect(withRoles.companies.map((c) => c.name)).toEqual(["Esri", "Peec AI"]);
    expect(notFound.companies.map((c) => c.name)).toEqual(["Ghost Co", "Zendesk"]);
  });

  it("drops groups that are empty", () => {
    const segments = segmentCompanies([company("A", "resolved", 3)]);
    expect(segments.map((s) => s.key)).toEqual(["withRoles"]);
  });
});

describe("ringArcs", () => {
  it("lays segments end to end around the ring, each shortened by the gap", () => {
    const segments = segmentCompanies(companies);
    const arcs = ringArcs(segments, 100, 2);
    // 6 counted companies over a 100-long ring: 2, 1, 1, 2 -> 33.3, 16.7, 16.7, 33.3 each less 2.
    expect(arcs.map((a) => Math.round(a.length * 10) / 10)).toEqual([31.3, 14.7, 14.7, 31.3]);
    expect(arcs[0].offset).toBe(0);
    expect(Math.round(arcs[1].offset * 10) / 10).toBe(33.3);
  });

  it("draws a single segment as the whole ring with no gap", () => {
    const arcs = ringArcs(segmentCompanies([company("A", "resolved", 3)]), 100, 2);
    expect(arcs).toEqual([{ key: "withRoles", length: 100, offset: 0 }]);
  });

  it("draws nothing for an empty list", () => {
    expect(ringArcs([], 100, 2)).toEqual([]);
  });
});

describe("CompanyDonut", () => {
  it("writes every segment's count and share in the legend", () => {
    render(<CompanyDonut companies={companies} />);
    const legend = screen.getByRole("list");
    expect(within(legend).getByText("Job board found, open roles")).toBeInTheDocument();
    expect(within(legend).getAllByText("2 · 33%")).toHaveLength(2);
    expect(within(legend).getAllByText("1 · 17%")).toHaveLength(2);
    expect(screen.getByRole("img", { name: "6 companies by job-board status" })).toBeInTheDocument();
  });

  it("opens the companies behind a legend entry, with their open roles", () => {
    render(<CompanyDonut companies={companies} />);
    expect(screen.queryByText("Esri")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Job board found, open roles/ }));
    expect(screen.getByText("Esri")).toBeInTheDocument();
    expect(screen.getByText("447 open roles")).toBeInTheDocument();
    expect(screen.queryByText("Ghost Co")).not.toBeInTheDocument();
  });

  it("opens the same list from the ring, and closes it on a second click", () => {
    const { container } = render(<CompanyDonut companies={companies} />);
    const arc = container.querySelector('[data-segment="notFound"]') as Element;
    fireEvent.click(arc);
    expect(screen.getByText("Ghost Co")).toBeInTheDocument();
    fireEvent.click(arc);
    expect(screen.queryByText("Ghost Co")).not.toBeInTheDocument();
  });

  it("keeps the ring out of the tab order: the legend is the keyboard control", () => {
    const { container } = render(<CompanyDonut companies={companies} />);
    expect(container.querySelectorAll("svg [tabindex], svg [role='button']")).toHaveLength(0);
    const entry = screen.getByRole("button", { name: /Could not be checked/ });
    entry.focus();
    expect(entry).toHaveFocus();
    fireEvent.click(entry);
    expect(screen.getByText("Slow Corp")).toBeInTheDocument();
  });

  it("keeps why a company was not found out of sight until asked", () => {
    render(<CompanyDonut companies={companies} />);
    fireEvent.click(screen.getByRole("button", { name: /No public job board found/ }));
    expect(screen.getByText("Ghost Co")).toBeInTheDocument();
    expect(screen.queryByText(/tried: ghost-co/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByText(/tried: ghost-co/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Hide details" }));
    expect(screen.queryByText(/tried: ghost-co/)).not.toBeInTheDocument();
  });

  it("closes the list with an X, back to the clean view", () => {
    render(<CompanyDonut companies={companies} />);
    fireEvent.click(screen.getByRole("button", { name: /No public job board found/ }));
    expect(screen.getByText("Ghost Co")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close list" }));
    expect(screen.queryByText("Ghost Co")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /No public job board found/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("offers the X for boards that were found too", () => {
    render(<CompanyDonut companies={companies} />);
    fireEvent.click(screen.getByRole("button", { name: /Job board found, open roles/ }));
    fireEvent.click(screen.getByRole("button", { name: "Close list" }));
    expect(screen.queryByText("Esri")).not.toBeInTheDocument();
  });

  it("offers no details toggle for boards that were found", () => {
    render(<CompanyDonut companies={companies} />);
    fireEvent.click(screen.getByRole("button", { name: /Job board found, open roles/ }));
    expect(screen.queryByRole("button", { name: "Show details" })).not.toBeInTheDocument();
  });

  it("renders nothing when there are no companies", () => {
    const { container } = render(<CompanyDonut companies={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("holds the ring's place while the counts are unknown", () => {
    render(<CompanyDonutSkeleton />);
    expect(screen.getByRole("status")).toHaveTextContent(
      "Working out how your company list resolved",
    );
    // The ring itself is decoration here: there is nothing yet for it to say.
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});
