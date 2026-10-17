"use client";

import { useState } from "react";

import { ringArcs, segmentCompanies, type SegmentKey } from "@/lib/company-segments";
import type { CompanyListSummary } from "@/lib/api";

const RADIUS = 46;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

/** How the company list resolved, as a ring plus a legend. The legend is the readable and
 * keyboard-operable form (every count is written out); the ring is the glance, and a
 * click on a slice is a mouse/touch shortcut to the same list of companies. The reason a
 * company was not found stays one click deeper. */
export function CompanyDonut({ companies }: { companies: CompanyListSummary["companies"] }) {
  const segments = segmentCompanies(companies);
  const total = segments.reduce((sum, segment) => sum + segment.count, 0);
  const [selected, setSelected] = useState<SegmentKey | null>(null);
  const [showWhy, setShowWhy] = useState(false);
  if (total === 0) return null;

  const arcs = ringArcs(segments, CIRCUMFERENCE, 2);
  const open = segments.find((segment) => segment.key === selected) ?? null;
  const toggle = (key: SegmentKey) => {
    setSelected((current) => (current === key ? null : key));
    setShowWhy(false);
  };

  return (
    <div className="company-donut">
      <div className="donut-row">
        <svg
          aria-label={`${total} companies by job-board status`}
          className="donut-ring"
          role="img"
          viewBox="0 0 120 120"
        >
          {arcs.map((arc) => {
            const segment = segments.find((item) => item.key === arc.key)!;
            const active = selected === arc.key;
            return (
              <circle
                className={`donut-arc donut-${arc.key}${active ? " donut-arc-active" : ""}${
                  selected && !active ? " donut-arc-dim" : ""
                }`}
                cx="60"
                cy="60"
                fill="none"
                key={arc.key}
                data-segment={arc.key}
                onClick={() => toggle(arc.key)}
                r={RADIUS}
                strokeDasharray={`${arc.length} ${CIRCUMFERENCE}`}
                strokeDashoffset={-arc.offset}
                transform="rotate(-90 60 60)"
              >
                <title>{`${segment.label}: ${segment.count}`}</title>
              </circle>
            );
          })}
          <text className="donut-total" textAnchor="middle" x="60" y="62">
            {total}
          </text>
          <text className="donut-caption" textAnchor="middle" x="60" y="77">
            companies
          </text>
        </svg>

        <ul className="donut-legend">
          {segments.map((segment) => (
            <li key={segment.key}>
              <button
                aria-pressed={selected === segment.key}
                className="donut-legend-item"
                onClick={() => toggle(segment.key)}
                type="button"
              >
                <span aria-hidden="true" className={`donut-swatch donut-fill-${segment.key}`} />
                <span className="donut-legend-label">{segment.label}</span>
                <span className="donut-legend-count">
                  {segment.count} · {Math.round((segment.count / total) * 100)}%
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      {open ? (
        <div className="donut-detail">
          <div className="donut-detail-head">
            <h3>
              {open.label} ({open.count})
            </h3>
            <div className="donut-detail-actions">
              {open.key === "blocked" || open.key === "notFound" ? (
                <button
                  aria-pressed={showWhy}
                  className="ghost-action"
                  onClick={() => setShowWhy((current) => !current)}
                  type="button"
                >
                  {showWhy ? "Hide details" : "Show details"}
                </button>
              ) : null}
              <button
                aria-label="Close list"
                className="donut-close"
                onClick={() => toggle(open.key)}
                type="button"
              >
                ✕
              </button>
            </div>
          </div>
          <ul className="donut-companies">
            {open.companies.map((company) => (
              <li key={company.name}>
                <span>{company.name}</span>
                {open.key === "withRoles" ? (
                  <span className="meta">{company.openRoles} open roles</span>
                ) : null}
                {showWhy && company.lastError ? (
                  <span className="meta donut-why">{company.lastError}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

/** The ring's shape while the counts are still unknown. The area a donut is about to
 * occupy says "this is being worked out" rather than reading as nothing there at all —
 * the real donut returns null on an empty list, which looks identical to absent. */
export function CompanyDonutSkeleton() {
  return (
    <div aria-busy="true" className="company-donut donut-skeleton">
      <div className="donut-row">
        <svg aria-hidden="true" className="donut-ring" viewBox="0 0 120 120">
          <circle
            className="donut-arc"
            cx="60"
            cy="60"
            fill="none"
            r={RADIUS}
            stroke="var(--seg-notFound)"
            strokeDasharray={`${CIRCUMFERENCE} 0`}
            strokeLinecap="butt"
          />
        </svg>
        <p className="meta" role="status">
          Working out how your company list resolved…
        </p>
      </div>
    </div>
  );
}
