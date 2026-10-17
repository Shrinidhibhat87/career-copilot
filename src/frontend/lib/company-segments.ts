import type { CompanyListSummary } from "@/lib/api";

export type CompanyEntry = CompanyListSummary["companies"][number];
export type SegmentKey = "withRoles" | "noRoles" | "blocked" | "notFound";

export type Segment = {
  key: SegmentKey;
  label: string;
  count: number;
  companies: CompanyEntry[];
};

const LABELS: Record<SegmentKey, string> = {
  withRoles: "Job board found, open roles",
  noRoles: "Job board found, no open roles",
  blocked: "Could not be checked",
  notFound: "No public job board found",
};

const ORDER: SegmentKey[] = ["withRoles", "noRoles", "blocked", "notFound"];

function keyOf(company: CompanyEntry): SegmentKey | null {
  if (company.status === "resolved") return company.openRoles === 0 ? "noRoles" : "withRoles";
  if (company.status === "blocked") return "blocked";
  if (company.status === "unresolved") return "notFound";
  return null; // inactive: no longer on the list
}

/** Groups the ledger into the four states a user cares about. Empty groups are dropped,
 * and companies within one are ordered by what the user most wants to see first. */
export function segmentCompanies(companies: CompanyEntry[]): Segment[] {
  const groups = new Map<SegmentKey, CompanyEntry[]>();
  for (const company of companies) {
    const key = keyOf(company);
    if (key) groups.set(key, [...(groups.get(key) ?? []), company]);
  }
  return ORDER.filter((key) => groups.has(key)).map((key) => {
    const members = [...(groups.get(key) ?? [])];
    members.sort((a, b) =>
      key === "withRoles"
        ? (b.openRoles ?? 0) - (a.openRoles ?? 0) || a.name.localeCompare(b.name)
        : a.name.localeCompare(b.name),
    );
    return { key, label: LABELS[key], count: members.length, companies: members };
  });
}

export type Arc = { key: SegmentKey; length: number; offset: number };

/** Stroke-dash geometry for a ring of the given circumference: each segment's visible
 * length (less a small gap, so neighbours never touch) and where along the ring it starts. */
export function ringArcs(segments: Segment[], circumference: number, gap: number): Arc[] {
  const total = segments.reduce((sum, segment) => sum + segment.count, 0);
  if (total === 0) return [];
  const spacing = segments.length > 1 ? gap : 0;
  let travelled = 0;
  return segments.map((segment) => {
    const share = (segment.count / total) * circumference;
    const arc = { key: segment.key, length: Math.max(share - spacing, 0.5), offset: travelled };
    travelled += share;
    return arc;
  });
}
